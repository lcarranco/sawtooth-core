# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import json
import base64
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web

# pylint: disable=no-name-in-module,import-error
# needed for the google.protobuf imports to pass pylint
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from google.protobuf.message import Message as BaseMessage

from sawtooth_sdk.client.exceptions import ValidatorConnectionError
from sawtooth_sdk.client.future import FutureTimeoutError
from sawtooth_sdk.client.stream import Stream
from sawtooth_sdk.protobuf.validator_pb2 import Message

import sawtooth_rest_api.exceptions as errors
import sawtooth_rest_api.error_handlers as error_handlers
from sawtooth_rest_api.protobuf import client_pb2
from sawtooth_rest_api.protobuf.block_pb2 import BlockHeader
from sawtooth_rest_api.protobuf.batch_pb2 import BatchList
from sawtooth_rest_api.protobuf.batch_pb2 import BatchHeader
from sawtooth_rest_api.protobuf.transaction_pb2 import TransactionHeader


DEFAULT_TIMEOUT = 300


class RouteHandler(object):
    """Contains a number of aiohttp handlers for endpoints in the Rest Api.

    Each handler takes an aiohttp Request object, and uses the data in
    that request to send Protobuf message to a validator. The Protobuf response
    is then parsed, and finally an aiohttp Response object is sent back
    to the client with JSON formatted data and metadata.

    If something goes wrong, an aiohttp HTTP exception is raised or returned
    instead.

    Args:
        stream_url (str): The TCP url to communitcate with the validator
        timeout (int, optional): The time in seconds before the Api should
            cancel a request and report that the validator is unavailable.
    """
    def __init__(self, loop, stream_url, timeout=DEFAULT_TIMEOUT):
        loop.set_default_executor(ThreadPoolExecutor())
        self._loop = loop
        self._stream = Stream(stream_url)
        self._timeout = timeout

    async def submit_batches(self, request):
        """Accepts a binary encoded BatchList and submits it to the validator.

        Request:
            body: octet-stream BatchList of one or more Batches
            query:
                - wait: Request should not return until all batches committed

        Response:
            status:
                 - 200: Batches submitted, but wait timed out before committed
                 - 201: All batches submitted and committed
                 - 202: Batches submitted and pending (not told to wait)
            data: Status of uncommitted batches (if any, when told to wait)
            link: /batches or /batch_status link for submitted batches

        """
        # Parse request
        if request.headers['Content-Type'] != 'application/octet-stream':
            return errors.WrongBodyType()

        payload = await request.read()
        if not payload:
            return errors.EmptyProtobuf()

        try:
            batch_list = BatchList()
            batch_list.ParseFromString(payload)
        except DecodeError:
            return errors.BadProtobuf()

        # Query validator
        error_traps = [error_handlers.InvalidBatch()]
        validator_query = client_pb2.ClientBatchSubmitRequest(
            batches=batch_list.batches)
        self._set_wait(request, validator_query)

        response = await self._query_validator(
            Message.CLIENT_BATCH_SUBMIT_REQUEST,
            client_pb2.ClientBatchSubmitResponse,
            validator_query,
            error_traps)

        # Build response envelope
        data = response['batch_statuses'] or None
        link = '{}://{}/batch_status?id={}'.format(
            request.scheme,
            request.host,
            ','.join(b.header_signature for b in batch_list.batches))

        if data is None:
            status = 202
        elif any(s != 'COMMITTED' for _, s in data.items()):
            status = 200
        else:
            status = 201
            data = None
            link = link.replace('batch_status', 'batches')

        return self._wrap_response(
            data=data,
            metadata={'link': link},
            status=status)

    async def list_statuses(self, request):
        """Fetches the committed status of batches by either a POST or GET.

        Request:
            body: A JSON array of one or more id strings (if POST)
            query:
                - id: A comma separated list of up to 15 ids (if GET)
                - wait: Request should not return until all batches committed

        Response:
            data: A JSON object, with batch ids as keys, and statuses as values
            link: The /batch_status link queried (if GET)
        """
        error_traps = [error_handlers.StatusesNotReturned()]

        # Parse batch ids from POST body, or query paramaters
        if request.method == 'POST':
            if request.headers['Content-Type'] != 'application/json':
                return errors.BadStatusBody()

            ids = await request.json()

            if not isinstance(ids, list):
                return errors.BadStatusBody()
            if len(ids) == 0:
                return errors.MissingStatusId()
            if not isinstance(ids[0], str):
                return errors.BadStatusBody()

        else:
            try:
                ids = request.url.query['id'].split(',')
            except KeyError:
                return errors.MissingStatusId()

        # Query validator
        validator_query = client_pb2.ClientBatchStatusRequest(batch_ids=ids)
        self._set_wait(request, validator_query)

        response = await self._query_validator(
            Message.CLIENT_BATCH_STATUS_REQUEST,
            client_pb2.ClientBatchStatusResponse,
            validator_query,
            error_traps)

        # Send response
        if request.method != 'POST':
            metadata = self._get_metadata(request, response)
        else:
            metadata = None

        return self._wrap_response(
            data=response.get('batch_statuses'),
            metadata=metadata)

    async def list_state(self, request):
        """Fetches list of data leaves, optionally filtered by address prefix.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - address: Return leaves whose addresses begin with this prefix

        Response:
            data: An array of leaf objects with address and data keys
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
            paging: Paging info and nav, like total resources and a next link
        """
        paging_controls = self._get_paging_controls(request)
        validator_query = client_pb2.ClientStateListRequest(
            head_id=request.url.query.get('head', None),
            address=request.url.query.get('address', None),
            paging=self._make_paging_message(paging_controls))

        response = await self._query_validator(
            Message.CLIENT_STATE_LIST_REQUEST,
            client_pb2.ClientStateListResponse,
            validator_query)

        return self._wrap_paginated_response(
            request=request,
            response=response,
            controls=paging_controls,
            data=response.get('leaves', []))

    async def fetch_state(self, request):
        """Fetches data from a specific address in the validator's state tree.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - address: The 70 character address of the data to be fetched

        Response:
            data: The base64 encoded binary data stored at that address
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
        """
        error_traps = [
            error_handlers.MissingLeaf(),
            error_handlers.BadAddress()]

        address = request.match_info.get('address', '')
        head = request.url.query.get('head', None)

        response = await self._query_validator(
            Message.CLIENT_STATE_GET_REQUEST,
            client_pb2.ClientStateGetResponse,
            client_pb2.ClientStateGetRequest(head_id=head, address=address),
            error_traps)

        return self._wrap_response(
            data=response['value'],
            metadata=self._get_metadata(request, response))

    async def list_blocks(self, request):
        """Fetches list of blocks from validator, optionally filtered by id.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - id: Comma separated list of block ids to include in results

        Response:
            data: JSON array of fully expanded Block objects
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
            paging: Paging info and nav, like total resources and a next link
        """
        paging_controls = self._get_paging_controls(request)
        validator_query = client_pb2.ClientBlockListRequest(
            head_id=request.url.query.get('head', None),
            block_ids=self._get_filter_ids(request),
            paging=self._make_paging_message(paging_controls))

        response = await self._query_validator(
            Message.CLIENT_BLOCK_LIST_REQUEST,
            client_pb2.ClientBlockListResponse,
            validator_query)

        return self._wrap_paginated_response(
            request=request,
            response=response,
            controls=paging_controls,
            data=[self._expand_block(b) for b in response['blocks']])

    async def fetch_block(self, request):
        """Fetches a specific block from the validator, specified by id.
        Request:
            path:
                - block_id: The 128-character id of the block to be fetched

        Response:
            data: A JSON object with the data from the fully expanded Block
            link: The link to this exact query
        """
        error_traps = [error_handlers.MissingBlock()]

        block_id = request.match_info.get('block_id', '')

        response = await self._query_validator(
            Message.CLIENT_BLOCK_GET_REQUEST,
            client_pb2.ClientBlockGetResponse,
            client_pb2.ClientBlockGetRequest(block_id=block_id),
            error_traps)

        return self._wrap_response(
            data=self._expand_block(response['block']),
            metadata=self._get_metadata(request, response))

    async def list_batches(self, request):
        """Fetches list of batches from validator, optionally filtered by id.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - id: Comma separated list of batch ids to include in results

        Response:
            data: JSON array of fully expanded Batch objects
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
            paging: Paging info and nav, like total resources and a next link
        """
        paging_controls = self._get_paging_controls(request)
        validator_query = client_pb2.ClientBatchListRequest(
            head_id=request.url.query.get('head', None),
            batch_ids=self._get_filter_ids(request),
            paging=self._make_paging_message(paging_controls))

        response = await self._query_validator(
            Message.CLIENT_BATCH_LIST_REQUEST,
            client_pb2.ClientBatchListResponse,
            validator_query)

        return self._wrap_paginated_response(
            request=request,
            response=response,
            controls=paging_controls,
            data=[self._expand_batch(b) for b in response['batches']])

    async def fetch_batch(self, request):
        """Fetches a specific batch from the validator, specified by id.

        Request:
            path:
                - batch_id: The 128-character id of the batch to be fetched

        Response:
            data: A JSON object with the data from the fully expanded Batch
            link: The link to this exact query
        """
        error_traps = [error_handlers.MissingBatch()]

        batch_id = request.match_info.get('batch_id', '')

        response = await self._query_validator(
            Message.CLIENT_BATCH_GET_REQUEST,
            client_pb2.ClientBatchGetResponse,
            client_pb2.ClientBatchGetRequest(batch_id=batch_id),
            error_traps)

        return self._wrap_response(
            data=self._expand_batch(response['batch']),
            metadata=self._get_metadata(request, response))

    async def list_transactions(self, request):
        """Fetches list of txns from validator, optionally filtered by id.

        Request:
            query:
                - head: The id of the block to use as the head of the chain
                - id: Comma separated list of txn ids to include in results

        Response:
            data: JSON array of Transaction objects with expanded headers
            head: The head used for this query (most recent if unspecified)
            link: The link to this exact query, including head block
            paging: Paging info and nav, like total resources and a next link
        """
        paging_controls = self._get_paging_controls(request)
        validator_query = client_pb2.ClientTransactionListRequest(
            head_id=request.url.query.get('head', None),
            transaction_ids=self._get_filter_ids(request),
            paging=self._make_paging_message(paging_controls))

        response = await self._query_validator(
            Message.CLIENT_TRANSACTION_LIST_REQUEST,
            client_pb2.ClientTransactionListResponse,
            validator_query)

        data = [self._expand_transaction(t) for t in response['transactions']]

        return self._wrap_paginated_response(
            request=request,
            response=response,
            controls=paging_controls,
            data=data)

    async def fetch_transaction(self, request):
        """Fetches a specific transaction from the validator, specified by id.

        Request:
            path:
                - transaction_id: The 128-character id of the txn to be fetched

        Response:
            data: A JSON object with the data from the expanded Transaction
            link: The link to this exact query
        """
        error_traps = [error_handlers.MissingTransaction()]

        txn_id = request.match_info.get('transaction_id', '')

        response = await self._query_validator(
            Message.CLIENT_TRANSACTION_GET_REQUEST,
            client_pb2.ClientTransactionGetResponse,
            client_pb2.ClientTransactionGetRequest(transaction_id=txn_id),
            error_traps)

        return self._wrap_response(
            data=self._expand_transaction(response['transaction']),
            metadata=self._get_metadata(request, response))

    async def _query_validator(self, request_type, response_proto,
                               content, traps=None):
        """Sends a request to the validator and parses the response.
        """
        response = await self._try_validator_request(request_type, content)
        return self._try_response_parse(response_proto, response, traps)

    async def _try_validator_request(self, message_type, content):
        """Serializes and sends a Protobuf message to the validator.
        Handles timeout errors as needed.
        """
        if isinstance(content, BaseMessage):
            content = content.SerializeToString()

        future = self._stream.send(message_type=message_type, content=content)

        try:
            response = await self._loop.run_in_executor(
                None,
                future.result,
                self._timeout)
        except FutureTimeoutError:
            raise errors.ValidatorUnavailable()

        try:
            return response.content
        # Caused by resolving a FutureError on validator disconnect
        except ValidatorConnectionError:
            raise errors.ValidatorUnavailable()

    @classmethod
    def _try_response_parse(cls, proto, response, traps=None):
        """Parses the Protobuf response from the validator.
        Uses "error traps" to send back any HTTP error triggered by a Protobuf
        status, both those common to many handlers, and specified individually.
        """
        parsed = proto()
        parsed.ParseFromString(response)
        traps = traps or []

        try:
            traps.append(error_handlers.Unknown(proto.INTERNAL_ERROR))
        except AttributeError:
            # Not every protobuf has every status enum, so pass AttributeErrors
            pass

        try:
            traps.append(error_handlers.NotReady(proto.NOT_READY))
        except AttributeError:
            pass

        try:
            traps.append(error_handlers.MissingHead(proto.NO_ROOT))
        except AttributeError:
            pass

        try:
            traps.append(error_handlers.InvalidPaging(proto.INVALID_PAGING))
        except AttributeError:
            pass

        for trap in traps:
            trap.check(parsed.status)

        return cls.message_to_dict(parsed)

    @staticmethod
    def _wrap_response(data=None, metadata=None, status=200):
        """Creates the JSON response envelope to be sent back to the client.
        """
        envelope = metadata or {}

        if data is not None:
            envelope['data'] = data

        return web.Response(
            status=status,
            content_type='application/json',
            text=json.dumps(
                envelope,
                indent=2,
                separators=(',', ': '),
                sort_keys=True))

    @classmethod
    def _wrap_paginated_response(cls, request, response, controls, data):
        """Builds the metadata for a pagingated response and wraps everying in
        a JSON encoded web.Response
        """
        head = response['head_id']
        link = cls._build_url(request, head)

        paging_response = response['paging']
        total = paging_response['total_resources']
        paging = {'total_count': total}

        # If there are no resources, there should be nothing else in paging
        if total == 0:
            return cls._wrap_response(
                data=data,
                metadata={'head': head, 'link': link, 'paging': paging})

        count = controls.get('count', len(data))
        start = paging_response['start_index']
        paging['start_index'] = start

        # Builds paging urls specific to this response
        def build_pg_url(min_pos=None, max_pos=None):
            return cls._build_url(request, head, count, min_pos, max_pos)

        # Build paging urls based on ids
        if 'start_id' in controls or 'end_id' in controls:
            if paging_response['next_id']:
                paging['next'] = build_pg_url(paging_response['next_id'])
            if paging_response['previous_id']:
                paging['previous'] = build_pg_url(
                    max_pos=paging_response['previous_id'])

        # Build paging urls based on indexes
        else:
            end_index = controls.get('end_index', None)
            if end_index is None and start + count < total:
                paging['next'] = build_pg_url(start + count)
            elif end_index is not None and end_index + 1 < total:
                paging['next'] = build_pg_url(end_index + 1)
            if start - count >= 0:
                paging['previous'] = build_pg_url(start - count)

        return cls._wrap_response(
            data=data,
            metadata={'head': head, 'link': link, 'paging': paging})

    @classmethod
    def _get_metadata(cls, request, response):
        """Parses out the head and link properties based on the HTTP Request
        from the client, and the Protobuf response from the validator.
        """
        head = response.get('head_id', None)
        metadata = {'link': cls._build_url(request, head)}

        if head is not None:
            metadata['head'] = head
        return metadata

    @classmethod
    def _build_url(cls, request, head=None, count=None,
                   min_pos=None, max_pos=None):
        """Builds a response URL to send back in response envelope.
        """
        query = request.url.query.copy()

        if head is not None:
            url = '{}://{}{}?head={}'.format(
                request.scheme,
                request.host,
                request.path,
                head)
            query.pop('head', None)
        else:
            return str(request.url)

        if min_pos is not None:
            url += '&{}={}'.format('min', min_pos)
        elif max_pos is not None:
            url += '&{}={}'.format('max', max_pos)
        else:
            queries = ['{}={}'.format(k, v) for k, v in query.items()]
            return url + '&' + '&'.join(queries) if queries else url

        url += '&{}={}'.format('count', count)
        query.pop('min', None)
        query.pop('max', None)
        query.pop('count', None)

        queries = ['{}={}'.format(k, v) for k, v in query.items()]
        return url + '&' + '&'.join(queries) if queries else url

    @classmethod
    def _expand_block(cls, block):
        """Deserializes a Block's header, and the header of its Batches.
        """
        cls._parse_header(BlockHeader, block)
        if 'batches' in block:
            block['batches'] = [cls._expand_batch(b) for b in block['batches']]
        return block

    @classmethod
    def _expand_batch(cls, batch):
        """Deserializes a Batch's header, and the header of its Transactions.
        """
        cls._parse_header(BatchHeader, batch)
        if 'transactions' in batch:
            batch['transactions'] = [
                cls._expand_transaction(t) for t in batch['transactions']]
        return batch

    @classmethod
    def _expand_transaction(cls, transaction):
        """Deserializes a Transaction's header.
        """
        return cls._parse_header(TransactionHeader, transaction)

    @classmethod
    def _parse_header(cls, header_proto, obj):
        """Deserializes a base64 encoded Protobuf header.
        """
        header = header_proto()
        header_bytes = base64.b64decode(obj['header'])
        header.ParseFromString(header_bytes)
        obj['header'] = cls.message_to_dict(header)
        return obj

    @staticmethod
    def _get_paging_controls(request):
        """Parses min, max, and/or count queries into A paging controls dict.
        """
        min_pos = request.url.query.get('min', None)
        max_pos = request.url.query.get('max', None)
        count = request.url.query.get('count', None)
        controls = {}

        if count == '0':
            raise errors.BadCount()
        elif count is not None:
            try:
                controls['count'] = int(count)
            except ValueError:
                raise errors.BadCount()

        if min_pos is not None:
            try:
                controls['start_index'] = int(min_pos)
            except ValueError:
                controls['start_id'] = min_pos

        elif max_pos is not None:
            try:
                controls['end_index'] = int(max_pos)
            except ValueError:
                controls['end_id'] = max_pos

        return controls

    @staticmethod
    def _make_paging_message(controls):
        """Turns a raw paging controls dict into Protobuf PagingControls.
        """
        count = controls.get('count', None)
        end_index = controls.get('end_index', None)

        # an end_index must be changed to start_index, possibly modifying count
        if end_index is not None:
            if count is None:
                start_index = 0
                count = end_index
            elif count > end_index + 1:
                start_index = 0
                count = end_index + 1
            else:
                start_index = end_index + 1 - count
        else:
            start_index = controls.get('start_index', None)

        return client_pb2.PagingControls(
            start_id=controls.get('start_id', None),
            end_id=controls.get('end_id', None),
            start_index=start_index,
            count=count)

    def _set_wait(self, request, validator_query):
        """Parses the `wait` query parameter, and sets the corresponding
        `wait_for_commit` and `timeout` properties in the validator query.
        """
        wait = request.url.query.get('wait', 'false')
        if wait.lower() != 'false':
            validator_query.wait_for_commit = True
            try:
                validator_query.timeout = int(wait)
            except ValueError:
                # By default, waits for 95% of REST API's configured timeout
                validator_query.timeout = int(self._timeout * 0.95)

    @staticmethod
    def _get_filter_ids(request):
        """Parses the `id` filter paramter from the url query.
        """
        filter_ids = request.url.query.get('id', None)
        return filter_ids and filter_ids.split(',')

    @staticmethod
    def message_to_dict(message):
        """Converts a Protobuf object to a python dict with desired settings.
        """
        return MessageToDict(
            message,
            including_default_value_fields=True,
            preserving_proto_field_name=True)
