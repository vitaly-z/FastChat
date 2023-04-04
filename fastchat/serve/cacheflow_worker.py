"""
A model worker executes the model based on Cacheflow.
"""
import argparse
import asyncio
import dataclasses
import logging
import json
import time
from typing import List, Union, Dict
import threading
import uuid
import torch
import uvicorn
import requests

import ray
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
from transformers import AutoTokenizer, AutoModelForCausalLM
from cacheflow.utils import Counter, get_gpu_memory, get_cpu_memory
from cacheflow.master.server import Server, initialize_ray_cluster
from cacheflow.sequence import Sequence, SequenceGroup
from cacheflow.sampling_params import SamplingParams
from fastchat.constants import WORKER_HEART_BEAT_INTERVAL
from fastchat.utils import build_logger, disable_torch_init, server_error_msg, pretty_print_semaphore

GB = 1 << 30

worker_id = str(uuid.uuid4())[:6]
logger = build_logger("model_worker", f"model_worker_{worker_id}.log")
global_counter = 0
seed = torch.cuda.current_device()


def heart_beat_worker(controller):

    while True:
        time.sleep(WORKER_HEART_BEAT_INTERVAL)
        controller.send_heart_beat()


def load_model(model_path, num_gpus):
    disable_torch_init()

    if num_gpus == 1:
        kwargs = {}
    else:
        kwargs = {
            "device_map": "auto",
            "max_memory": {i: "13GiB" for i in range(num_gpus)},
        }

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(
       model_path, torch_dtype=torch.float16, **kwargs)

    if num_gpus == 1:
        model.cuda()

    if hasattr(model.config, "max_sequence_length"):
        context_len = model.config.max_sequence_length
    else:
        context_len = 2048

    return tokenizer, model, context_len


class CacheFlowWorker:
    def __init__(self,
                 controller_addr,
                 worker_addr,
                 worker_id,
                 no_register,
                 model_path,
                 model_name,
                 num_gpus,
                 block_size,
                 seed,
                 swap_space,
                 max_batch_size,
                 distributed_init_method,
                 all_stage_devices):
        self.controller_addr = controller_addr
        self.worker_addr = worker_addr
        self.worker_id = worker_id
        if model_path.endswith("/"):
            model_path = model_path[:-1]
        self.model_name = model_name or model_path.split("/")[-1]

        logger.info(f"Loading the model {self.model_name} on worker {worker_id} ...")
        # self.tokenizer, self.model, self.context_len = load_model(
            # model_path, num_gpus)
        self.block_size = block_size

        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.seq_group_counter = Counter()
        self.seq_counter = Counter()
        self.context_len = 2048

        # Note(Hao): here are hard-coded parameters
        # pipeline_parallel_size = 1,
        # tensor_parallel_size = 1,
        # dtype = torch.float16,
        #
        # remote_server_class = ray.remote(num_cpus=0)(Server)
        remote_server_class = Server
        self.server = remote_server_class(
            model=self.model_name,
            model_path=model_path,
            pipeline_parallel_size=1,
            tensor_parallel_size=1,
            block_size=block_size,
            dtype=torch.float16,
            seed=seed,
            swap_space=swap_space,
            max_batch_size=max_batch_size,
            num_nodes=1,
            num_devices_per_node=4,
            distributed_init_method=distributed_init_method,
            all_stage_devices=all_stage_devices,
            gpu_memory=get_gpu_memory(),
            cpu_memory=get_cpu_memory(),
        )
        self.running_seq_groups: Dict[int, SequenceGroup] = {}
        self.sequence_group_events: Dict[int, asyncio.Event] = {}
        self.is_server_running = False

        if not no_register:
            self.register_to_controller()
            self.heart_beat_thread = threading.Thread(
                target=heart_beat_worker, args=(self,))
            self.heart_beat_thread.start()

    def register_to_controller(self):
        logger.info("Register to controller")

        url = self.controller_addr + "/register_worker"
        data = {
            "worker_name": self.worker_addr,
            "check_heart_beat": True,
            "worker_status": self.get_status()
        }
        r = requests.post(url, json=data)
        assert r.status_code == 200

    def send_heart_beat(self):
        logger.info(f"Send heart beat. Models: {[self.model_name]}. "
                    f"Semaphore: {pretty_print_semaphore(model_semaphore)}. "
                    f"global_counter: {global_counter}")

        url = self.controller_addr + "/receive_heart_beat"

        while True:
            try:
                ret = requests.post(url, json={
                    "worker_name": self.worker_addr,
                    "queue_length": self.get_queue_length()}, timeout=5)
                exist = ret.json()["exist"]
                break
            except requests.exceptions.RequestException as e:
                logger.error(f"heart beat error: {e}")
            time.sleep(5)

        if not exist:
            self.register_to_controller()

    def get_queue_length(self):
        if model_semaphore is None:
            return 0
        else:
            return args.limit_model_concurrency - model_semaphore._value + len(
                model_semaphore._waiters)

    def get_status(self):
        return {
            "model_names": [self.model_name],
            "speed": 1,
            "queue_length": self.get_queue_length(),
        }

    def server_step(self):
        self.is_server_running = True
        updated_seq_groups = self.server.step()
        self.is_server_running = False
        for seq_group in updated_seq_groups:
            group_id = seq_group.group_id
            self.running_seq_groups[group_id] = seq_group
            # self.sequence_group_events[group_id].set()

    # @torch.inference_mode()
    def generate_stream(self, params):
        #cur_mem = torch.cuda.memory_allocated()
        #max_mem = torch.cuda.max_memory_allocated()
        #logging.info(f"cur mem: {cur_mem/GB:.2f} GB, max_mem: {max_mem/GB:.2f} GB")

        tokenizer = self.tokenizer

        context = params["prompt"]
        temperature = float(params.get("temperature", 1.0))
        max_new_tokens = min(int(params.get("max_new_tokens", 256)), 1024)
        stop_str = params.get("stop", None)

        input_ids = tokenizer(context).input_ids
        output_ids = list(input_ids)

        max_src_len = self.context_len - max_new_tokens - 8
        input_ids = input_ids[-max_src_len:]

        # make sampling params in cacheflow
        sampling_params = SamplingParams.from_dict(params)
        sampling_params.stop_token_ids.add(tokenizer.eos_token_id)
        sampling_params.n = 1
        sampling_params.max_num_steps = max_new_tokens
        print(f"==========stop str: {stop_str}=========")
        if stop_str is not None:
            sampling_params.stop_func = stop_str
        # we might sample multiple sequences, but in chatbot, this is one
        seqs: List[Sequence] = []
        for _ in range(sampling_params.n):
            seq_id = next(self.seq_counter)
            seq = Sequence(seq_id, input_ids, block_size=self.block_size)
            seqs.append(seq)

        arrival_time = time.time()
        group_id = next(self.seq_group_counter)
        seq_group = SequenceGroup(group_id, seqs, arrival_time)
        # group_event = asyncio.Event()
        # self.sequence_group_events[group_id] = group_event
        self.server.add_sequence_groups([(seq_group, sampling_params)])
        while True:
            if not self.is_server_running:
                self.server_step()
            # await asyncio.wait_for(group_event.wait(), timeout=1)
            # group_event.clear()
            seq_group = self.running_seq_groups[group_id]
            all_outputs = []
            for seq in seq_group.seqs:
                token_ids = seq.get_token_ids()
                output = self.tokenizer.decode(token_ids, skip_special_tokens=True)
                if stop_str is not None:
                    if output.endswith(stop_str):
                        output = output[:-len(stop_str)]
                all_outputs.append(output)
            assert len(seq_group.seqs) == 1
            ret = {
                "text": all_outputs[0],
                "error_code": 0,
            }
            yield (json.dumps(ret) + "\0").encode("utf-8")
            if seq_group.is_finished():
                break


            # if all_outputs[0][-len(stop_str):] == stop_str:
            #     print(all_outputs[0][-len(stop_str):])
            #     break




        # for i in range(max_new_tokens):
        #     if i == 0:
        #         out = model(
        #             torch.as_tensor([input_ids]).cuda(), use_cache=True)
        #         logits = out.logits
        #         past_key_values = out.past_key_values
        #     else:
        #         attention_mask = torch.ones(
        #             1, past_key_values[0][0].shape[-2] + 1, device="cuda")
        #         out = model(input_ids=torch.as_tensor([[token]], device="cuda"),
        #                     use_cache=True,
        #                     attention_mask=attention_mask,
        #                     past_key_values=past_key_values)
        #         logits = out.logits
        #         past_key_values = out.past_key_values
        #
        #     last_token_logits = logits[0][-1]
        #     if temperature < 1e-4:
        #         token = int(torch.argmax(last_token_logits))
        #     else:
        #         probs = torch.softmax(last_token_logits / temperature, dim=-1)
        #         token = int(torch.multinomial(probs, num_samples=1))
        #
        #     output_ids.append(token)
        #     output = tokenizer.decode(output_ids, skip_special_tokens=True)
        #     if output.endswith(stop_str):
        #         output = output[:-len(stop_str)]
        #         stopped = True
        #     elif token == tokenizer.eos_token_id:
        #         stopped = True
        #     else:
        #         stopped = False
        #
        #     if i % args.stream_interval == 0 or i == max_new_tokens - 1 or stopped:
        #         ret = {
        #             "text": output,
        #             "error_code": 0,
        #         }
        #         yield json.dumps(ret).encode() + b"\0"
        #
        #     if stopped:
        #         break
        #
        # del past_key_values

    def generate_stream_gate(self, params):
        try:
            for x in self.generate_stream(params):
                yield x
        except torch.cuda.OutOfMemoryError:
            ret = {
                "text": server_error_msg,
                "error_code": 1,
            }
            yield json.dumps(ret).encode() + b"\0"

app = FastAPI()
model_semaphore = None


def release_model_semaphore():
    model_semaphore.release()


@app.post("/worker_generate_stream")
async def generate_stream(request: Request):
    global model_semaphore, global_counter
    global_counter += 1
    params = await request.json()

    if model_semaphore is None:
        model_semaphore = asyncio.Semaphore(args.limit_model_concurrency)
    await model_semaphore.acquire()
    generator = worker.generate_stream_gate(params)
    background_tasks = BackgroundTasks()
    background_tasks.add_task(release_model_semaphore)
    return StreamingResponse(generator, background=background_tasks)


@app.post("/worker_get_status")
async def get_status(request: Request):
    return worker.get_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=21002)
    parser.add_argument("--worker-address", type=str,
        default="http://localhost:21002")
    parser.add_argument("--controller-address", type=str,
        default="http://localhost:21001")
    parser.add_argument("--model-path", type=str, default="/home/haozhang/weights/hf-llama-7b")
    parser.add_argument("--model-name", type=str)
    parser.add_argument("--num-gpus", type=int, default=1)
    parser.add_argument("--limit-model-concurrency", type=int, default=4)
    parser.add_argument("--stream-interval", type=int, default=2)
    parser.add_argument("--no-register", action="store_true")
    # cacheflow specific params
    parser.add_argument('--block-size', type=int, default=8, choices=[8, 16],
                        help='token block size')
    parser.add_argument('--swap-space', type=int, default=20,
                        help='CPU swap space size (GiB) per GPU')
    parser.add_argument('--max-batch-size', type=int, default=2560,
                        help='maximum number of batched tokens')
    args = parser.parse_args()

    (num_nodes, num_devices_per_node, distributed_init_method,
    all_stage_devices) = initialize_ray_cluster(
            pipeline_parallel_size=1, tensor_parallel_size=1)

    worker = CacheFlowWorker(args.controller_address,
                             args.worker_address,
                             worker_id,
                             args.no_register,
                             args.model_path,
                             args.model_name,
                             args.num_gpus,
                             args.block_size,
                             seed,
                             args.swap_space,
                             args.max_batch_size,
                             distributed_init_method,
                             all_stage_devices)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
