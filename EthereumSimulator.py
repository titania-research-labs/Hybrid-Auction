import asyncio
from collections import defaultdict
from itertools import islice
import json
import logging
from web3 import Web3
from web3 import AsyncWeb3
import aiohttp  
from dataclasses import dataclass
from json import JSONEncoder

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
tx_memo = defaultdict(lambda: defaultdict(int))
node_list = [
    "http://localhost:8545",
    "http://localhost:8546",
    "http://localhost:8547",
    "http://localhost:8548",
    "http://localhost:8549"
]

@dataclass
class TransactionData:
    tx_hash: str
    priority_fee: int
    direct_transfer: int
    tx_profit: int
    internal_txs_length: int
    gas_used: int
    is_revert: bool

@dataclass
class SimulationResult:
    builder: str
    old_balance: int
    new_balance: int
    profit: int
    priority_fee: int
    direct_transfer: int
    revert_count: int
    reverted_tx_hashes: list[str]
    per_tx_detail_list: list[TransactionData]

@dataclass
class TobRobSimulationResult:
    block_hash: str
    tob_profit: int
    rob_profit: int
    tob_revert_count: int
    rob_revert_count: int
    full_block: SimulationResult

class CustomJSONEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (TobRobSimulationResult, SimulationResult, TransactionData)):
            return obj.__dict__
        elif isinstance(obj, bytes):
            return obj.hex()
        return super().default(obj)





class EvmSimulator:
    def __init__(self, rpc_url: str, session: aiohttp.ClientSession):
        self.rpc_url = rpc_url
        self.w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        self.snapshot_id = None
        self.session = session  # aiohttp.ClientSession
 
    async def before_simulate(self, wallet_address: str):
        self.wallet_address = Web3.to_checksum_address(wallet_address)
        await self.reset_state()
        await self._set_coinbase()
        self.snapshot_id = await self._evm_snapshot()
 
    async def _evm_snapshot(self) -> str:

        payload = {
            "jsonrpc": "2.0",
             "method": "evm_snapshot",
             "params": [],
             "id": 1,
        }
        async with self.session.post(self.rpc_url, json=payload) as response:
            res = await response.json()
            return res.get('result')
 
    async def _evm_revert(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "evm_revert",
            "params": [self.snapshot_id],
            "id": 1,
        }
        async with self.session.post(self.rpc_url, json=payload) as response:
            await response.json()
        self.snapshot_id = await self._evm_snapshot()
 
    async def _set_coinbase(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "anvil_setCoinbase",
            "params": [self.wallet_address],
            "id": 1,
        }
        async with self.session.post(self.rpc_url, json=payload) as response:
             await response.json()
 
    async def _set_next_block_timestamp(self, timestamp: int) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "evm_setNextBlockTimestamp",
            "params": [timestamp],
            "id": 1,
        }
        async with self.session.post(self.rpc_url, json=payload) as response:
            await response.json()
 
    async def _mine_block(self) -> None:
        payload = {
            "jsonrpc": "2.0",
             "method": "evm_mine",
             "params": [],
             "id": 1,
         }
        async with self.session.post(self.rpc_url, json=payload) as response:
             await response.json()
 
    async def _decode_raw_transaction(self, raw_transaction: str) -> dict[str, any]:
         # simulate asynchroneous command
         command = ["cast", "decode-transaction", raw_transaction]
         process = await asyncio.create_subprocess_exec(
             *command,
             stdout=asyncio.subprocess.PIPE,
             stderr=asyncio.subprocess.PIPE
         )
         stdout, stderr = await process.communicate()
         if process.returncode != 0:
             print(f"Error decoding transaction: {stderr.decode()}")
             return {}
         return json.loads(stdout.decode())
 
    async def _parse_internal_tx(self, tx_hash: str) -> list[dict[str, any]]:
       
        try:
            internal_txs = []
            payload = {
                "jsonrpc": "2.0",
                "method": "debug_traceTransaction",
                "params": [tx_hash, {"tracer": "callTracer"}],
                "id": 1,
            }

            async with self.session.post(self.rpc_url, json=payload) as response:
                trace = await response.json()


            def recurse_calls(call: dict[str, any]) -> None:
                tx = {
                    'from': call.get('from'),
                    'to': call.get('to'),
                    'value': int(call.get('value', '0x0'), 16),
                    'input': call.get('input'),
                    'type': call.get('type'),
                }

                internal_txs.append(tx)
                for sub_call in call.get('calls', []):
                    recurse_calls(sub_call)
    
            if 'result' in trace:
                recurse_calls(trace['result'])
            else:
                print(f"Error tracing transaction {tx_hash}: {trace.get('error')}")
            return internal_txs
        except Exception as e:
            print(f"An error occurs when analyzing internal transaction: {e} ")

    def _calc_builder_profit(self, internal_txs: list[dict[str, any]]) -> int:
         builder_address = self.wallet_address.lower()
         return sum(tx.get('value', 0) for tx in internal_txs if tx.get('to', '').lower() == builder_address)
 
    async def simulate(self, block_data: dict[str, any], transactions: list[str]) -> SimulationResult:
        execution_payload = block_data['payload']['execution_payload']
        await self.before_simulate(execution_payload["fee_recipient"])
        logger.info(f"-- Starting simulation for block: {execution_payload['block_hash']}")

        base_fee_per_gas = int(execution_payload.get('base_fee_per_gas', '0'))
        # block_timestamp = int(execution_payload.get('timestamp', '0'))

        old_balance = await self.w3.eth.get_balance(self.wallet_address)
        logger.info(f"--- Initial balance: {old_balance}")

        already_sended = []
        priority_fee = 0
        direct_transfer = 0
        revert_count = 0

        logger.info(f"---- Processing {len(transactions)} transactions")
        for i, raw_tx in enumerate(transactions, 1):
            tx_hash = None
            await asyncio.sleep(0.03)
            try:
                tx_hash = await self.w3.eth.send_raw_transaction(bytes.fromhex(raw_tx[2:]))
                already_sended.append((tx_hash.hex(), raw_tx))
                tx_memo[self.rpc_url][tx_hash.hex()] += 1
            except Exception as e:
                if tx_hash is not None:
                    tx_memo[self.rpc_url][tx_hash.hex()] += 1
                    print(f"{self.rpc_url}---{tx_memo[self.rpc_url][tx_hash.hex()]}🤩 im corrent" if tx_memo[self.rpc_url][tx_hash.hex()] > 0 else "🤯 you wrong")
                logger.error(f"👻Fail to send tx {i}/{len(transactions)}: {e}\n")
                already_sended.append((None, raw_tx))
                
        
        # await self._set_next_block_timestamp(block_timestamp)

        # mine blocks
        await self._mine_block()
        logger.info("Block mined")

        reverted_tx_hashes = []
        tx_data: list[TransactionData] = []

        logger.info("Processing transaction results")
        for i, (tx_hash, raw_tx) in enumerate(already_sended, 1):
            if tx_hash is None:
                revert_count += 1
                empty_tx = TransactionData(
                    tx_hash="",
                    priority_fee=0,
                    direct_transfer=0,
                    tx_profit=0,
                    internal_txs_length=0,
                    gas_used=0,
                    is_revert=True
                )
                tx_data.append(empty_tx)
                continue

            encoded_tx = await self._decode_raw_transaction(raw_tx)
            try:
                tx = await self.w3.eth.get_transaction(tx_hash)
                receipt = await self.w3.eth.get_transaction_receipt(tx_hash)

            except Exception as e:
                logger.error(f"❌{self.rpc_url}:  Error fetching data for transaction {i}/{len(already_sended)}: {tx_hash}\n{e}\n{tx_memo[self.rpc_url][tx_hash.hex()]}")
                revert_count += 1
                continue

            is_revert = receipt["status"] == 0
            if is_revert:
                revert_count += 1
                reverted_tx_hashes.append(tx_hash)

            gas_used = receipt['gasUsed'] 
            if encoded_tx["type"] == "0x2":
                 if "maxFeePerGas" in tx:
                     max_fee = tx["maxFeePerGas"]
                     max_priority_fee = int(encoded_tx["maxPriorityFeePerGas"], 16)
                     priority_fee += min(max_priority_fee, (max_fee - base_fee_per_gas)) * gas_used
                 else:
                     print("maxFeePerGas not found")
            else:
                 max_fee = tx["gasPrice"]
                 priority_fee += (max_fee - base_fee_per_gas) * gas_used
            
            
            tx_data.append(TransactionData(
                 tx_hash=tx_hash,
                 priority_fee=priority_fee,
                 direct_transfer=direct_transfer,
                 tx_profit=priority_fee + direct_transfer,
                 internal_txs_length=len(await self._parse_internal_tx(tx_hash)),
                 gas_used=gas_used,
                 is_revert=is_revert
             ))
        

        new_balance = await self.w3.eth.get_balance(self.wallet_address)
        profit = new_balance - old_balance

        direct_transfer = max(profit - priority_fee, 0)

        print(f"💰💰💰: direct_transfer: {direct_transfer}")

        await self.reset_state()

        return SimulationResult(
            builder=self.wallet_address,
            old_balance=old_balance,
            new_balance=new_balance,
            profit=profit,
            priority_fee=priority_fee,
            direct_transfer=direct_transfer,
            revert_count=revert_count,
            reverted_tx_hashes=reverted_tx_hashes,
            per_tx_detail_list=tx_data
        )
 
    # async def simulate_tob_rob(self, block: Dict[str, Any], tob, rob) -> TobRobSimulationResult:
    #     try:
    #         transactions = tob + rob
    #         logger.info(f"- Simulating ToB/RoB for block {block['payload']['execution_payload']['block_hash']}")
    #         results = await self.simulate(block, transactions)
    #         tx_data = results.per_tx_detail_list
    #         tob_profit = sum(tx.tx_profit+tx.direct_transfer for tx in tx_data[:len(tob)])
    #         rob_profit = sum(tx.tx_profit+tx.direct_transfer for tx in tx_data[len(tob):])
    #         tob_revert_count = sum(1 for tx in tx_data[:len(tob)] if tx.is_revert)
    #         rob_revert_count = sum(1 for tx in tx_data[len(tob):] if tx.is_revert)

    #         logger.info(f"ToB profit: {tob_profit}, RoB profit: {rob_profit}")
    #         logger.info(f"ToB revert count: {tob_revert_count}, RoB revert count: {rob_revert_count}")

    #         return TobRobSimulationResult(
    #             block_hash=block["payload"]["execution_payload"]["block_hash"],
    #             tob_profit=tob_profit,
    #             rob_profit=rob_profit,
    #             tob_revert_count=tob_revert_count,
    #             rob_revert_count=rob_revert_count,
    #             full_block=results
    #         )
    #     except Exception as e:
    #         await self.reset_state()

    async def simulate_tob_rob(self, block: dict[str, any], tob, rob) -> TobRobSimulationResult:
        try:
            tob_result = await self.simulate(block, tob)
            print("tob: 👉",tob_result.builder[:5],tob_result.old_balance)
            rob_result = await self.simulate(block, rob)
            print("rob: 👉",tob_result.builder[:5],rob_result.old_balance)
            return TobRobSimulationResult(
                block_hash=block["payload"]["execution_payload"]["block_hash"],
                tob_profit=tob_result.profit,
                rob_profit=rob_result.profit,
                tob_revert_count=tob_result.revert_count,
                rob_revert_count=rob_result.revert_count,
                full_block=rob_result
            )
        except:
            await self.reset_state()
            empty_result = TobRobSimulationResult(
                block_hash=block["payload"]["execution_payload"]["block_hash"],
                tob_profit=0,
                rob_profit=0,
                tob_revert_count=0,
                rob_revert_count=0,
                full_block=SimulationResult(
                    builder="",
                    old_balance=0,
                    new_balance=0,
                    profit=0,
                    priority_fee=0,
                    direct_transfer=0,
                    revert_count=0,
                    reverted_tx_hashes=[],
                    per_tx_detail_list=[]
                )
            )
            return empty_result
        
    async def reset_state(self) -> None:
        await self._evm_revert()


# ================================================================================================




def select_block(file_path: str) -> list[dict[str, any]]:
    with open(file_path, 'r') as file:
        blocks = [json.loads(line) for line in file]
    
    reversed_blocks = list(reversed(blocks))
    selected_blocks = []
    used_builder = defaultdict(bool)
    
    for block in reversed_blocks:
        fee_recipient = block.get('payload', {}).get('execution_payload', {}).get('fee_recipient', '')
        if not used_builder[fee_recipient]:
            selected_blocks.append(block)
            used_builder[fee_recipient] = True
    
    return selected_blocks

def select_all_blocks(file_path: str) -> list[dict[str, any]]:
    with open(file_path, 'r') as file:
        return [json.loads(line) for line in file]


def read_blocks_from_file(file_path: str) -> list[dict[str, any]]:
    with open(file_path, 'r') as file:
        return [json.loads(line) for line in file]

def simple(block: dict[str, any]) -> tuple[list[str], list[str]]:
    transactions = block['payload']['execution_payload']['transactions']
    return transactions[:5], transactions[5:]

def chunked_iterable(iterable, size):
    it = iter(iterable)
    return iter(lambda: list(islice(it, size)), [])


async def main():
    file_path = ""
    # blocks = select_block(file_path)
    blocks = select_all_blocks(file_path)
    print(f"there is {len(blocks)} blocks")

    async with aiohttp.ClientSession() as session:
        rest_blocks_count = len(blocks)
        simulators = [EvmSimulator(node_list[i], session) for i in range(len(node_list))]
        print(f"------ 💻: {len(simulators)} simulators is ready!! ---------")
        print(f"------ 🧱: there is {len(blocks)} blocks --------")
        biggest_tob_block = ("", 0, {})
        biggest_rob_block = ("", 0, {})
        all_results: list[TobRobSimulationResult] = []
        try:
            print("start simulation!!!!")
            # divide blocks into chunks. Chunks number is the same as the number of simulators.
            for chunk in chunked_iterable(blocks, len(simulators)):
                for i in range(min(len(simulators), len(chunk))):
                    await simulators[i].before_simulate(chunk[i]["payload"]["execution_payload"]["fee_recipient"])
    
                tasks = []
                rest_blocks_count -= len(chunk)
                for sim, block in zip(simulators, chunk):
                    print("-", (await sim.w3.eth.get_block('latest'))["number"])
                    tob,rob = simple(block)
                    task = asyncio.create_task(sim.simulate_tob_rob(block, tob, rob))
                    tasks.append(task)

                if tasks:
                    # wait for all tasks to complete
                    results = await asyncio.gather(*tasks)
                    for result in results:
                        if result.tob_profit > biggest_tob_block[1]:
                            biggest_tob_block = (result.block_hash, result.tob_profit, result)
                        if result.rob_profit > biggest_rob_block[1]:
                            biggest_rob_block = (result.block_hash, result.rob_profit, result)
                        all_results.append(result)
                    print("🔥🔥🔥 Find Biggest ToB/RoB 🔥🔥🔥")
                    print(f"biggest_tob_block: {biggest_tob_block[0]}")
                    print(f"biggest_rob_block: {biggest_rob_block[0]}")
            
            print(biggest_tob_block)
            print(biggest_rob_block)

 
        except Exception as e:
            print("error: ", e)

        finally:
            ## write json file from all_results
            with open("all_results2.json", "w") as file:
                json.dump(all_results, file, cls=CustomJSONEncoder)
 


if __name__ == "__main__":
    asyncio.run(main())

    