***************************************
Config Transaction Family Specification 
***************************************

Overview
=========

The config transaction family provides a methodology for storing on-chain
configuration settings.

The settings stored in state as a result of this transaction family play a
critical role in the operation of a validator. For example, the consensus
module uses these settings; in the case of PoET, one cross-network setting is
target wait time (which must be the same across validators), and this setting
is stored as sawtooth.poet.target_wait_time.  Other parts of the system use
these settings similarly; for example, the list of enabled transaction
families is used by the transaction processing platform.

In addition, pluggable components such as transaction family implementations
can use the settings during their execution.

This design supports three authorization options: a) no authorization (only
appropriate for testing), b) a single authorized key which can make changes,
and c) multiple authorized keys.  In the case of multiple keys, a percentage
of the keys are required to make a change.

.. note::

	While usable in a production sense, this transaction family also serves as
	a reference specification and implementation.  The authorization scheme
	here provides a simple voting mechanism; another authorization scheme may
	be better in practice.  Implementations which use a different
	authorization scheme can replace this implementation by adhering to the
	same addressing scheme and content format for settings.  (For example, the
	location of PoET settings can not change, or the PoET consensus module
	will not be able to find them.)


State
=====

This section describes in detail how config settings are stored and addressed using
the config transaction family. 

The configuration data consists of setting/value pairs. A setting is the name
for the item of configuration data. The value is the data in the form of a string. 

Settings
--------

Settings are namespaced using dots:

=============================================				============
Setting (Examples)												Value
=============================================				============
sawtooth.poet.target_wait_time	  				 			5
sawtooth.validator.max_transactions_per_block				1000
=============================================				============


The config transaction family uses the following settings for its own configuration:

+------------------------------------------+-------------------------------------------------------------+
| Setting (Config)                         | Value Description                                           |
+==========================================+=============================================================+
| sawtooth.config.authorization_type       | One of ['None, 'Ballot'].   Default is 'None'.              |
+------------------------------------------+-------------------------------------------------------------+
| sawtooth.config.vote.authorized_keys     | List of public keys allowed to vote.                        |
+------------------------------------------+-------------------------------------------------------------+
| sawtooth.config.vote.approval_threshold  | Percentage of keys required for a proposal to be accepted.  |
+------------------------------------------+-------------------------------------------------------------+
| sawtooth.config.vote.proposals           | A list of proposals to make configuration changes (see note)|
+------------------------------------------+-------------------------------------------------------------+

.. note::
	*sawtooth.config.vote.proposals* is a base64 encoded string of the 
	protobuf message *ConfigCandidates*. This setting cannot be modified
	by a proposal or a vote.


Definition of Setting Entries
-----------------------------

The following protocol buffers definition defines setting entries:

.. code-block:: protobuf
	:caption: File: sawtooth-core/core_transactions/config/protos/config.proto

	// Setting Container for the resulting state
	message Setting {
	    // Contains a setting entry (or entries, in the case of collisions).
	    message Entry {
	        string key = 1;
	        string value = 2;
	    }

	    // List of setting entries - more than one implies a state key collision
	    repeated Entry entries = 1;
	}

sawtooth.config.vote.proposals 
------------------------------

The setting 'sawtooth.config.vote.proposals' is stored as defined by the
following protocol buffers definition. The value returned by this  setting is
a base64 encoded *ConfigCandidates* message:

.. code-block:: protobuf

	// Contains the vote counts for a given proposal.
	message ConfigCandidate {
	    // An individual vote record
	    message VoteRecord {
	        // The public key of the voter
	        string public_key = 1;

	        // The voter's actual vote
	        ConfigVote.Vote vote = 2;

	    }

	    // The proposal id, a hash of the original proposal
	    string proposal_id = 1;

	    // The active propsal
	    ConfigProposal proposal = 2;

	    // list of votes
	    repeated VoteRecord votes = 3;
	}

	// Contains all the configuration candiates up for vote.
	message ConfigCandidates {
	    repeated ConfigCandidate candidates = 1;
	}


Addressing
----------

When a setting is read or changed, it is accessed by addressing it using the following algorithm:

Addresses for the config transaction family are set by adding a sha256 hash 
of the setting name to the config namespace of '000000'. For example, the 
setting *sawtooth.config.vote.proposals* could be set like this:

.. code-block:: pycon

	>>> '000000' + hashlib.sha256('sawtooth.config.vote.proposals').hexdigest()
	'000000041706776ff37b8d2a75450422d8bdbe894f6988b012ae0a5ec751434eadc014'


Transaction Payload
===================

Config transaction family payloads are defined by the following protocol
buffers code:

.. code-block:: protobuf
	:caption: File: sawtooth-core/core_transactions/config/protos/config.proto

	// Configuration Setting Payload
	// - Contains either a propsal or a vote.
	message ConfigPayload {
	    // The action indicates data is contained within this payload
	    enum Action {
	        // A proposal action - data will be a ConfigProposal
	        PROPOSE = 0;

	        // A vote action - data will be a ConfigVote
	        VOTE = 1;
	    }
	    // The action of this payload
	    Action action = 1;

	    // The content of this payload
	    bytes data = 2;
	}

	// Configuration Setting Proposal
	//
	// This message proposes a change in a setting value.
	message ConfigProposal {
	    // The setting key.  E.g. sawtooth.config.authorization_type
	    string setting = 1;

	    // The setting value. E.g. 'ballot'
	    string value = 2;

	    // allow duplicate proposals with different hashes
	    // randomly created by the client
	    string nonce = 3;
	}

	// Configuration Setting Vote
	//
	// In ballot mode, a propsal must be voted on.  This message indicates an
	// acceptance or rejection of a proposal, where the proposal is identified
	// by its id.
	message ConfigVote {
	    enum Vote {
	        ACCEPT = 0;
	        REJECT = 1;
	    }

	    // The id of the proposal, as found in the
	    // sawtooth.config.vote.proposals setting field
	    string proposal_id = 1;

	    Vote vote = 2;
	}


Transaction Header
==================

Inputs and Outputs
------------------

The inputs for config family transactions must include:

* the address of *sawtooth.config.authorization_type*
* the address of *sawtooth.config.vote.proposals*
* the address of *sawtooth.config.vote.authorized_keys*
* the address of *sawtooth.config.vote.approval_threshold*
* the address of the setting being changed

The outputs for config family transactions must include:

* the address of *sawtooth.config.vote.proposals*
* the address of the setting being changed


Dependencies
------------

None.


Family 
------

- family_name: "sawtooth_config"
- family_version: "1.0"

Encoding
--------

The encoding field must be set to "application/protobuf".


Execution
=========



