from __future__ import annotations

from copy import deepcopy
from typing import List, Optional, Set, Union

from pycardano.address import Address
from pycardano.backend.base import ChainContext
from pycardano.coinselection import (
    LargestFirstSelector,
    RandomImproveMultiAsset,
    UTxOSelector,
)
from pycardano.exception import (
    InsufficientUTxOBalanceException,
    InvalidArgumentException,
    InvalidTransactionException,
    TransactionBuilderException,
    UTxOSelectionException,
)
from pycardano.hash import ScriptDataHash, ScriptHash, VerificationKeyHash
from pycardano.key import ExtendedSigningKey, SigningKey, VerificationKey
from pycardano.logging import logger
from pycardano.metadata import AuxiliaryData
from pycardano.nativescript import NativeScript, ScriptAll, ScriptAny, ScriptPubkey
from pycardano.plutus import Datum, ExecutionUnits, Redeemer, datum_hash
from pycardano.transaction import (
    Asset,
    AssetName,
    MultiAsset,
    Transaction,
    TransactionBody,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.utils import fee, max_tx_fee, min_lovelace, script_data_hash
from pycardano.witness import TransactionWitnessSet, VerificationKeyWitness

__all__ = ["TransactionBuilder"]

FAKE_VKEY = VerificationKey.from_primitive(
    bytes.fromhex(
        "58205e750db9facf42b15594790e3ac882ed5254eb214a744353a2e24e4e65b8ceb4"
    )
)

# Ed25519 signature of a 32-bytes message (TX hash) will have length of 64
FAKE_TX_SIGNATURE = bytes.fromhex(
    "7a40e127815e62595e8de6fdeac6dd0346b8dbb0275dca5f244b8107cff"
    "e9f9fd8de14b60c3fdc3409e70618d8681afb63b69a107eb1af15f8ef49edb4494001"
)


class TransactionBuilder:
    """A class builder that makes it easy to build a transaction.

    Args:
        context (ChainContext): A chain context.
        utxo_selectors (Optional[List[UTxOSelector]]): A list of UTxOSelectors that will select input UTxOs.
    """

    def __init__(
        self, context: ChainContext, utxo_selectors: Optional[List[UTxOSelector]] = None
    ):
        self.context = context
        self._inputs = []
        self._excluded_inputs = []
        self._input_addresses = []
        self._outputs = []
        self._fee = 0
        self._ttl = None
        self._validity_start = None
        self._auxiliary_data = None
        self._native_scripts = None
        self._mint = None
        self._required_signers = None
        self._scripts = []
        self._datums = []
        self._redeemers = []
        self._inputs_to_redeemers = {}
        self._collaterals = []

        if utxo_selectors:
            self.utxo_selectors = utxo_selectors
        else:
            self.utxo_selectors = [RandomImproveMultiAsset(), LargestFirstSelector()]

    def add_input(self, utxo: UTxO) -> TransactionBuilder:
        """Add a specific UTxO to transaction's inputs.

        Args:
            utxo (UTxO): UTxO to be added.

        Returns:
            TransactionBuilder: Current transaction builder.
        """
        self.inputs.append(utxo)
        return self

    def add_script_input(
        self, utxo: UTxO, script: bytes, datum: Datum, redeemer: Redeemer
    ) -> TransactionBuilder:
        """Add a script UTxO to transaction's inputs.

        Args:
            utxo (UTxO): Script UTxO to be added.
            script (Optional[bytes]): A plutus script.
            datum (Optional[Datum]): A plutus datum to unlock the UTxO.
            redeemer (Optional[Redeemer]): A plutus redeemer to unlock the UTxO.

        Returns:
            TransactionBuilder: Current transaction builder.
        """
        if not utxo.output.address.address_type.name.startswith("SCRIPT"):
            raise InvalidArgumentException(
                f"Expect the output address of utxo to be script type, "
                f"but got {utxo.output.address.address_type} instead."
            )
        if utxo.output.datum_hash != datum.hash():
            raise InvalidArgumentException(
                f"Datum hash in transaction output is {utxo.output.datum_hash}, "
                f"but actual datum hash from input datum is {datum.hash()}."
            )
        self.scripts.append(script)
        self.datums.append(datum)
        self.redeemers.append(redeemer)
        self._inputs_to_redeemers[utxo] = redeemer
        self.inputs.append(utxo)
        return self

    def add_input_address(self, address: Union[Address, str]) -> TransactionBuilder:
        """Add an address to transaction's input address.
        Unlike :meth:`add_input`, which deterministically adds a UTxO to the transaction's inputs, `add_input_address`
        will not immediately select any UTxO when called. Instead, it will delegate UTxO selection to
        :class:`UTxOSelector`s of the builder when :meth:`build` is called.

        Args:
            address (Union[Address, str]): Address to be added.

        Returns:
            TransactionBuilder: The current transaction builder.
        """
        self.input_addresses.append(address)
        return self

    def add_output(
        self,
        tx_out: TransactionOutput,
        datum: Optional[Datum] = None,
        add_datum_to_witness: bool = False,
    ) -> TransactionBuilder:
        """Add a transaction output.

        Args:
            tx_out (TransactionOutput): The transaction output to be added.
            datum (Datum): Attach a datum hash to this transaction output.
            add_datum_to_witness (bool): Optionally add the actual datum to transaction witness set. Defaults to False.

        Returns:
            TransactionBuilder: Current transaction builder.
        """
        if datum:
            tx_out.datum_hash = datum_hash(datum)
        self.outputs.append(tx_out)
        if add_datum_to_witness:
            self.datums.append(datum)
        return self

    @property
    def inputs(self) -> List[UTxO]:
        return self._inputs

    @property
    def excluded_inputs(self) -> List[UTxO]:
        return self._excluded_inputs

    @excluded_inputs.setter
    def excluded_inputs(self, excluded_inputs: List[UTxO]):
        self._excluded_inputs = excluded_inputs

    @property
    def input_addresses(self) -> List[Union[Address, str]]:
        return self._input_addresses

    @property
    def outputs(self) -> List[TransactionOutput]:
        return self._outputs

    @property
    def fee(self) -> int:
        return self._fee

    @fee.setter
    def fee(self, fee: int):
        self._fee = fee

    @property
    def ttl(self) -> int:
        return self._ttl

    @ttl.setter
    def ttl(self, ttl: int):
        self._ttl = ttl

    @property
    def mint(self) -> MultiAsset:
        return self._mint

    @mint.setter
    def mint(self, mint: MultiAsset):
        self._mint = mint

    @property
    def auxiliary_data(self) -> AuxiliaryData:
        return self._auxiliary_data

    @auxiliary_data.setter
    def auxiliary_data(self, data: AuxiliaryData):
        self._auxiliary_data = data

    @property
    def native_scripts(self) -> List[NativeScript]:
        return self._native_scripts

    @native_scripts.setter
    def native_scripts(self, scripts: List[NativeScript]):
        self._native_scripts = scripts

    @property
    def validity_start(self):
        return self._validity_start

    @validity_start.setter
    def validity_start(self, validity_start: int):
        self._validity_start = validity_start

    @property
    def required_signers(self) -> List[VerificationKeyHash]:
        return self._required_signers

    @required_signers.setter
    def required_signers(self, signers: List[VerificationKeyHash]):
        self._required_signers = signers

    @property
    def scripts(self) -> List[bytes]:
        return self._scripts

    @property
    def datums(self) -> List[Datum]:
        return self._datums

    @property
    def redeemers(self) -> List[Redeemer]:
        return self._redeemers

    @property
    def collaterals(self) -> List[UTxO]:
        return self._collaterals

    @collaterals.setter
    def collaterals(self, collaterals: List[UTxO]):
        self._collaterals = collaterals

    @property
    def script_data_hash(self) -> Optional[ScriptDataHash]:
        if self.datums or self.redeemers:
            return script_data_hash(self.redeemers, self.datums)
        else:
            return None

    def _calc_change(
        self, fees, inputs, outputs, address, precise_fee=False
    ) -> List[TransactionOutput]:
        requested = Value(fees)
        for o in outputs:
            requested += o.amount

        provided = Value()
        for i in inputs:
            provided += i.output.amount
        if self.mint:
            provided.multi_asset += self.mint

        if not requested < provided:
            raise InvalidTransactionException(
                f"The input UTxOs cannot cover the transaction outputs and tx fee. \n"
                f"Inputs: {inputs} \n"
                f"Outputs: {outputs} \n"
                f"fee: {fees}"
            )

        change = provided - requested
        if change.coin < 0:
            # We assign max fee for now to ensure enough balance regardless of splits condition
            # We can implement a more precise fee logic and requirements later
            raise InsufficientUTxOBalanceException("Not enough ADA to cover fees")

        # Remove any asset that has 0 quantity
        if change.multi_asset:
            change.multi_asset = change.multi_asset.filter(lambda p, n, v: v > 0)

        change_output_arr = []

        # when there is only ADA left, simply use remaining coin value as change
        if not change.multi_asset:
            lovelace_change = change.coin
            change_output_arr.append(TransactionOutput(address, lovelace_change))

        # If there are multi asset in the change
        if change.multi_asset:
            # Split assets if size exceeds limits
            multi_asset_arr = self._pack_tokens_for_change(
                address, change, self.context.protocol_param.max_val_size
            )

            # Include minimum lovelace into each token output except for the last one
            for i, multi_asset in enumerate(multi_asset_arr):
                # Combine remainder of provided ADA with last MultiAsset for output
                # There may be rare cases where adding ADA causes size exceeds limit
                # We will revisit if it becomes an issue
                if (
                    precise_fee
                    and change.coin - min_lovelace(Value(0, multi_asset), self.context)
                    < 0
                ):
                    raise InsufficientUTxOBalanceException(
                        "Not enough ADA left to cover non-ADA assets in a change address"
                    )

                if i == len(multi_asset_arr) - 1:
                    # Include all ada in last output
                    change_value = Value(change.coin, multi_asset)
                else:
                    change_value = Value(0, multi_asset)
                    change_value.coin = min_lovelace(change_value, self.context)

                change_output_arr.append(TransactionOutput(address, change_value))
                change -= change_value
                change.multi_asset = change.multi_asset.filter(lambda p, n, v: v > 0)

        return change_output_arr

    def _add_change_and_fee(
        self, change_address: Optional[Address]
    ) -> TransactionBuilder:
        original_outputs = self.outputs[:]
        if change_address:
            # Set fee to max
            self.fee = max_tx_fee(self.context)
            changes = self._calc_change(
                self.fee, self.inputs, self.outputs, change_address, precise_fee=False
            )
            self._outputs += changes

        plutus_execution_units = ExecutionUnits(0, 0)
        for redeemer in self.redeemers:
            plutus_execution_units += redeemer.ex_units

        self.fee = fee(
            self.context,
            len(self._build_full_fake_tx().to_cbor("bytes")),
            plutus_execution_units.steps,
            plutus_execution_units.mem,
        )

        if change_address:
            self._outputs = original_outputs
            changes = self._calc_change(
                self.fee, self.inputs, self.outputs, change_address, precise_fee=True
            )
            self._outputs += changes

        return self

    def _adding_asset_make_output_overflow(
        self,
        output: TransactionOutput,
        current_assets: Asset,
        policy_id: ScriptHash,
        add_asset_name: AssetName,
        add_asset_val: int,
        max_val_size: int,
    ) -> bool:
        """Check if adding the asset will make output exceed maximum size limit

        Args:
            output (TransactionOutput): current output
            current_assets (Asset): current Assets to be included in output
            policy_id (ScriptHash): policy id containing the MultiAsset
            asset_to_add (Asset): Asset to add to current MultiAsset to check size limit

        """
        attempt_assets = deepcopy(current_assets)
        attempt_assets += Asset({add_asset_name: add_asset_val})
        attempt_multi_asset = MultiAsset({policy_id: attempt_assets})

        new_amount = Value(0, attempt_multi_asset)
        current_amount = deepcopy(output.amount)
        attempt_amount = new_amount + current_amount

        # Calculate minimum ada requirements for more precise value size
        required_lovelace = min_lovelace(attempt_amount, self.context)
        attempt_amount.coin = required_lovelace

        return len(attempt_amount.to_cbor("bytes")) > max_val_size

    def _pack_tokens_for_change(
        self,
        change_address: Optional[Address],
        change_estimator: Value,
        max_val_size: int,
    ) -> List[MultiAsset]:
        multi_asset_arr = []
        base_coin = Value(coin=change_estimator.coin)
        output = TransactionOutput(change_address, base_coin)

        # iteratively add tokens to output
        for (policy_id, assets) in change_estimator.multi_asset.items():
            temp_multi_asset = MultiAsset()
            temp_value = Value(coin=0)
            temp_assets = Asset()
            old_amount = deepcopy(output.amount)
            for asset_name, asset_value in assets.items():
                if self._adding_asset_make_output_overflow(
                    output,
                    temp_assets,
                    policy_id,
                    asset_name,
                    asset_value,
                    max_val_size,
                ):
                    # Insert current assets as one group if current assets isn't null
                    # This handles edge case when first Asset from next policy will cause overflow
                    if temp_assets:
                        temp_multi_asset += MultiAsset({policy_id: temp_assets})
                        temp_value.multi_asset = temp_multi_asset
                        output.amount += temp_value
                    multi_asset_arr.append(output.amount.multi_asset)

                    # Create a new output
                    base_coin = Value(coin=0)
                    output = TransactionOutput(change_address, base_coin)

                    # Continue building output from where we stopped
                    old_amount = deepcopy(output.amount)
                    temp_multi_asset = MultiAsset()
                    temp_value = Value()
                    temp_assets = Asset()

                temp_assets += Asset({asset_name: asset_value})

            # Assess assets in buffer
            temp_multi_asset += MultiAsset({policy_id: temp_assets})
            temp_value.multi_asset = temp_multi_asset
            output.amount += temp_value

            # Calculate min lovelace required for more precise size
            updated_amount = deepcopy(output.amount)
            required_lovelace = min_lovelace(updated_amount, self.context)
            updated_amount.coin = required_lovelace

            if len(updated_amount.to_cbor("bytes")) > max_val_size:
                output.amount = old_amount
                break

        multi_asset_arr.append(output.amount.multi_asset)
        # Remove records where MultiAsset is null due to overflow of adding
        # items at the beginning of next policy to previous policy MultiAssets
        return multi_asset_arr

    def _input_vkey_hashes(self) -> Set[VerificationKeyHash]:
        results = set()
        for i in self.inputs + self.collaterals:
            if isinstance(i.output.address.payment_part, VerificationKeyHash):
                results.add(i.output.address.payment_part)
        return results

    def _native_scripts_vkey_hashes(self) -> Set[VerificationKeyHash]:
        results = set()

        def _dfs(script: NativeScript):
            tmp = set()
            if isinstance(script, ScriptPubkey):
                tmp.add(script.key_hash)
            elif isinstance(script, (ScriptAll, ScriptAny)):
                for s in script.native_scripts:
                    tmp.update(_dfs(s))
            return tmp

        if self.native_scripts:
            for script in self.native_scripts:
                results.update(_dfs(script))

        return results

    def _set_redeemer_index(self):
        for i, utxo in enumerate(self.inputs):
            if utxo in self._inputs_to_redeemers:
                self._inputs_to_redeemers[utxo].index = i
        self.redeemers.sort(key=lambda r: r.index)

    def _build_tx_body(self) -> TransactionBody:
        tx_body = TransactionBody(
            [i.input for i in self.inputs],
            self.outputs,
            fee=self.fee,
            ttl=self.ttl,
            mint=self.mint,
            auxiliary_data_hash=self.auxiliary_data.hash()
            if self.auxiliary_data
            else None,
            script_data_hash=self.script_data_hash,
            required_signers=self.required_signers,
            validity_start=self.validity_start,
            collateral=[c.input for c in self.collaterals]
            if self.collaterals
            else None,
        )
        return tx_body

    def _build_fake_vkey_witnesses(self) -> List[VerificationKeyWitness]:
        vkey_hashes = self._input_vkey_hashes()
        vkey_hashes.update(self._native_scripts_vkey_hashes())
        return [
            VerificationKeyWitness(FAKE_VKEY, FAKE_TX_SIGNATURE) for _ in vkey_hashes
        ]

    def _build_fake_witness_set(self) -> TransactionWitnessSet:
        witness_set = self.build_witness_set()
        witness_set.vkey_witnesses = self._build_fake_vkey_witnesses()
        return witness_set

    def _build_full_fake_tx(self) -> Transaction:
        tx_body = self._build_tx_body()
        witness = self._build_fake_witness_set()
        tx = Transaction(tx_body, witness, True, self.auxiliary_data)
        size = len(tx.to_cbor("bytes"))
        if size > self.context.protocol_param.max_tx_size:
            raise InvalidTransactionException(
                f"Transaction size ({size}) exceeds the max limit "
                f"({self.context.protocol_param.max_tx_size}). Please try reducing the "
                f"number of inputs or outputs."
            )
        return tx

    def build_witness_set(self) -> TransactionWitnessSet:
        """Build a transaction witness set, excluding verification key witnesses.
        This function is especially useful when the transaction involves Plutus scripts.

        Returns:
            TransactionWitnessSet: A transaction witness set without verification key witnesses.
        """
        return TransactionWitnessSet(
            native_scripts=self.native_scripts,
            plutus_script=self.scripts if self.scripts else None,
            redeemer=self.redeemers if self.redeemers else None,
            plutus_data=self.datums if self.datums else None,
        )

    def _ensure_no_input_exclusion_conflict(self):
        intersection = set(self.inputs).intersection(set(self.excluded_inputs))
        if intersection:
            raise TransactionBuilderException(
                f"Found common UTxOs between UTxO inputs and UTxO excluded_inputs: "
                f"{intersection}."
            )

    def build(self, change_address: Optional[Address] = None) -> TransactionBody:
        """Build a transaction body from all constraints set through the builder.

        Args:
            change_address (Optional[Address]): Address to which changes will be returned. If not provided, the
                transaction body will likely be unbalanced (sum of inputs is greater than the sum of outputs).

        Returns:
            TransactionBody: A transaction body.
        """
        self._ensure_no_input_exclusion_conflict()
        selected_utxos = []
        selected_amount = Value()
        for i in self.inputs:
            selected_utxos.append(i)
            selected_amount += i.output.amount
        if self.mint:
            selected_amount.multi_asset += self.mint

        requested_amount = Value()
        for o in self.outputs:
            requested_amount += o.amount

        # Trim off assets that are not requested because they will be returned as changes eventually.
        trimmed_selected_amount = Value(
            selected_amount.coin,
            selected_amount.multi_asset.filter(
                lambda p, n, v: p in requested_amount.multi_asset
                and n in requested_amount.multi_asset[p]
            ),
        )

        unfulfilled_amount = requested_amount - trimmed_selected_amount
        unfulfilled_amount.coin = max(0, unfulfilled_amount.coin)
        # Clean up all non-positive assets
        unfulfilled_amount.multi_asset = unfulfilled_amount.multi_asset.filter(
            lambda p, n, v: v > 0
        )

        # When there are positive coin or native asset quantity in unfulfilled Value
        if Value() < unfulfilled_amount:
            additional_utxo_pool = []
            for address in self.input_addresses:
                for utxo in self.context.utxos(str(address)):
                    if utxo not in selected_utxos and utxo not in self.excluded_inputs:
                        additional_utxo_pool.append(utxo)

            for i, selector in enumerate(self.utxo_selectors):
                try:
                    selected, _ = selector.select(
                        additional_utxo_pool,
                        [TransactionOutput(None, unfulfilled_amount)],
                        self.context,
                    )
                    for s in selected:
                        selected_amount += s.output.amount
                        selected_utxos.append(s)

                    break

                except UTxOSelectionException as e:
                    if i < len(self.utxo_selectors) - 1:
                        logger.info(e)
                        logger.info(f"{selector} failed. Trying next selector.")
                    else:
                        raise UTxOSelectionException("All UTxO selectors failed.")

        selected_utxos.sort(
            key=lambda utxo: (str(utxo.input.transaction_id), utxo.input.index)
        )

        self.inputs[:] = selected_utxos[:]

        self._set_redeemer_index()

        self._add_change_and_fee(change_address)

        tx_body = self._build_tx_body()

        return tx_body

    def build_and_sign(
        self,
        signing_keys: List[Union[SigningKey, ExtendedSigningKey]],
        change_address: Optional[Address] = None,
    ) -> Transaction:
        """Build a transaction body from all constraints set through the builder and sign the transaction with
        provided signing keys.

        Args:
            signing_keys (List[Union[SigningKey, ExtendedSigningKey]]): A list of signing keys that will be used to
                sign the transaction.
            change_address (Optional[Address]): Address to which changes will be returned. If not provided, the
                transaction body will likely be unbalanced (sum of inputs is greater than the sum of outputs).

        Returns:
            Transaction: A signed transaction.
        """

        tx_body = self.build(change_address=change_address)
        witness_set = self.build_witness_set()
        witness_set.vkey_witnesses = []

        for signing_key in signing_keys:
            signature = signing_key.sign(tx_body.hash())
            witness_set.vkey_witnesses.append(
                VerificationKeyWitness(signing_key.to_verification_key(), signature)
            )

        return Transaction(tx_body, witness_set, auxiliary_data=self.auxiliary_data)
