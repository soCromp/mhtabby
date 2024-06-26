# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from collections import defaultdict
import copy
import json
import os
from os.path import exists, join, isdir
from dataclasses import dataclass, field
import sys
from typing import Optional, Dict, Sequence, List
import numpy as np
from tqdm import tqdm
import logging
import bitsandbytes as bnb
import pandas as pd
import importlib
from packaging import version
from packaging.version import parse
import warnings
from sklearn.metrics.pairwise import manhattan_distances
from torchmetrics.functional.pairwise import pairwise_manhattan_distance as manhattan
from torchmetrics.functional.pairwise import pairwise_cosine_similarity as cossim

import torch
import transformers
from torch.nn.utils.rnn import pad_sequence
import argparse
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    AutoModelForMaskedLM,
    set_seed,
    Seq2SeqTrainer,
    BitsAndBytesConfig,
    LlamaTokenizer,
    LlamaConfig

)
from datasets import load_dataset, Dataset, load_from_disk, DatasetDict
import evaluate

from peft import (
    prepare_model_for_kbit_training,
    LoraConfig,
    get_peft_model,
    PeftModel
)
from peft.tuners.lora import LoraLayer
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR

from transformers.modeling_utils import unwrap_model
from transformers.models.auto.modeling_auto import MODEL_FOR_CAUSAL_LM_MAPPING_NAMES
from transformers.utils import is_peft_available
from peft import PeftModel

from multihead_models import MultiheadLlamaForCausalLM, MHLlamaConfig

from transformers.integrations import WandbCallback

from datetime import datetime


def is_ipex_available():
    def get_major_and_minor_from_version(full_version):
        return str(version.parse(full_version).major) + "." + str(version.parse(full_version).minor)

    _torch_version = importlib.metadata.version("torch")
    if importlib.util.find_spec("intel_extension_for_pytorch") is None:
        return False
    _ipex_version = "N/A"
    try:
        _ipex_version = importlib.metadata.version("intel_extension_for_pytorch")
    except importlib.metadata.PackageNotFoundError:
        return False
    torch_major_and_minor = get_major_and_minor_from_version(_torch_version)
    ipex_major_and_minor = get_major_and_minor_from_version(_ipex_version)
    if torch_major_and_minor != ipex_major_and_minor:
        warnings.warn(
            f"Intel Extension for PyTorch {ipex_major_and_minor} needs to work with PyTorch {ipex_major_and_minor}.*,"
            f" but PyTorch {_torch_version} is found. Please switch to the matching version and run again."
        )
        return False
    return True
    

if torch.cuda.is_available():   
    torch.backends.cuda.matmul.allow_tf32 = True

logger = logging.getLogger(__name__)

IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"

@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(
        default="EleutherAI/pythia-12b"
    )
    task: Optional[str] = field(
        default='causal',
        metadata={'help': 'Task: set to causal if using llama model, or masked for Bert etc'}
    )
    trust_remote_code: Optional[bool] = field(
        default=False,
        metadata={"help": "Enable unpickling of arbitrary code in AutoModelForCausalLM#from_pretrained."}
    )
    num_heads: Optional[int] = field(
        default=1,
        metadata={'help': 'Number of heads (>=1) to put on the model. 1 results in normal model, more constructs multiheaded model.'}
    )

@dataclass
class DataArguments:
    eval_dataset_size: int = field(
        default=1024, metadata={"help": "Size of validation dataset."}
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    source_max_len: int = field(
        default=1024,
        metadata={"help": "Maximum source sequence length. Sequences will be right padded (and possibly truncated)."},
    )
    target_max_len: int = field(
        default=256,
        metadata={"help": "Maximum target sequence length. Sequences will be right padded (and possibly truncated)."},
    )
    dataset: str = field(
        default='alpaca',
        metadata={"help": "Which dataset to finetune on. See datamodule for options."}
    )
    dataset_format: Optional[str] = field(
        default="mlm",
        metadata={"help": "Which dataset format is used. [mlm|inout]"}
    )

@dataclass
class TrainingArguments(transformers.Seq2SeqTrainingArguments):
    train_on_source: Optional[bool] = field(
        default=False,
        metadata={"help": "Whether to train on the input in addition to the target text."}
    )
    mmlu_split: Optional[str] = field(
        default='eval',
        metadata={"help": "The MMLU split to run on"}
    )
    mmlu_dataset: Optional[str] = field(
        default='mmlu-fs',
        metadata={"help": "MMLU dataset to use: options are `mmlu-zs` for zero-shot or `mmlu-fs` for few shot."}
    )
    do_mmlu_eval: Optional[bool] = field(
        default=False,
        metadata={"help": "Whether to run the MMLU evaluation."}
    )
    max_mmlu_samples: Optional[int] = field(
        default=None,
        metadata={"help": "If set, only evaluates on `max_mmlu_samples` of the MMMLU dataset."}
    )
    mmlu_source_max_len: int = field(
        default=2048,
        metadata={"help": "Maximum source sequence length for mmlu."}
    )
    eval_samples: bool = field(
        default=False,
        metadata={"help": "Whether to produce text samples at each eval."}
    )
    full_finetune: bool = field(
        default=False,
        metadata={"help": "Finetune the entire model without adapters."}
    )
    diversity: bool = field(
        default = False,
        metadata={'help': 'Whether to include diversity term in the loss function.'}
    )
    divc1: int = field(
        default= 100,
        metadata={'help': "For diversity loss term, the constant by which distance matrix is divided."}
    )
    divc2: int = field(
        default=5,
        metadata={'help': "For diversity loss term, the constant by which the overall term is divided."}
    )
    divdist: str = field(
        default='manhattan',
        metadata={'help': 'Distance metric for diversity term. Should be one of: manhattan, cosine'}
    )
    adam8bit: bool = field(
        default=False,
        metadata={"help": "Use 8-bit adam."}
    )
    double_quant: bool = field(
        default=True,
        metadata={"help": "Compress the quantization statistics through double quantization."}
    )
    quant_type: str = field(
        default="nf4",
        metadata={"help": "Quantization data type to use. Should be one of `fp4` or `nf4`."}
    )
    bits: int = field(
        default=4,
        metadata={"help": "How many bits to use."}
    )
    lora_r: int = field(
        default=64,
        metadata={"help": "Lora R dimension."}
    )
    lora_alpha: float = field(
        default=16,
        metadata={"help": " Lora alpha."}
    )
    lora_dropout: float = field(
        default=0.0,
        metadata={"help":"Lora dropout."}
    )
    max_memory_MB: int = field(
        default=80000,
        metadata={"help": "Free memory per gpu."}
    )
    report_to: str = field(
        default='none',
        metadata={"help": "To use wandb or something else for reporting."}
    )
    output_dir: str = field(default='./output', metadata={"help": 'The output dir for logs and checkpoints'})
    optim: str = field(default='paged_adamw_32bit', metadata={"help": 'The optimizer to be used'})
    per_device_train_batch_size: int = field(default=1, metadata={"help": 'The training batch size per GPU. Increase for better speed.'})
    gradient_accumulation_steps: int = field(default=16, metadata={"help": 'How many gradients to accumulate before to perform an optimizer step'})
    max_steps: int = field(default=10000, metadata={"help": 'How many optimizer update steps to take'})
    weight_decay: float = field(default=0.0, metadata={"help": 'The L2 weight decay rate of AdamW'}) # use lora dropout instead for regularization if needed
    learning_rate: float = field(default=0.0002, metadata={"help": 'The learnign rate'})
    remove_unused_columns: bool = field(default=False, metadata={"help": 'Removed unused columns. Needed to make this codebase work.'})
    max_grad_norm: float = field(default=0.3, metadata={"help": 'Gradient clipping max norm. This is tuned and works well for all models tested.'})
    gradient_checkpointing: bool = field(default=True, metadata={"help": 'Use gradient checkpointing. You want to use this.'})
    do_train: bool = field(default=True, metadata={"help": 'To train or not to train, that is the question?'})
    do_generate: bool = field(default=True, metadata={"help": 'To gen or not to gen, that is the question?'})
    lr_scheduler_type: str = field(default='constant', metadata={"help": 'Learning rate schedule. Constant a bit better than cosine, and has advantage for analysis'})
    warmup_ratio: float = field(default=0.03, metadata={"help": 'Fraction of steps to do a warmup for'})
    logging_steps: int = field(default=10, metadata={"help": 'The frequency of update steps after which to log the loss'})
    group_by_length: bool = field(default=True, metadata={"help": 'Group sequences into batches with same length. Saves memory and speeds up training considerably.'})
    save_strategy: str = field(default='steps', metadata={"help": 'When to save checkpoints'})
    save_steps: int = field(default=250, metadata={"help": 'How often to save a model'})
    save_total_limit: int = field(default=40, metadata={"help": 'How many checkpoints to save before the oldest is overwritten'})

@dataclass
class GenerationArguments:
    # For more hyperparameters check:
    # https://huggingface.co/docs/transformers/main_classes/text_generation#transformers.GenerationConfig
    # Length arguments
    max_new_tokens: Optional[int] = field(
        default=10000,
        metadata={"help": "Maximum number of new tokens to be generated in evaluation or prediction loops"
                          "if predict_with_generate is set."}
    )
    min_new_tokens : Optional[int] = field(
        default=None,
        metadata={"help": "Minimum number of new tokens to generate."}
    )
    max_column_len: Optional[int] = field(
        default = 15,
        metadata={"help": "Max new tokens to generate *per column* in each row."}
    )

    # Generation strategy
    do_sample: Optional[bool] = field(default=False)
    num_beams: Optional[int] = field(default=1)
    num_beam_groups: Optional[int] = field(default=1)
    penalty_alpha: Optional[float] = field(default=None)
    use_cache: Optional[bool] = field(default=True)

    # Hyperparameters for logit manipulation
    temperature: Optional[float] = field(default=1.0)
    top_k: Optional[int] = field(default=50)
    top_p: Optional[float] = field(default=1.0)
    typical_p: Optional[float] = field(default=1.0)
    diversity_penalty: Optional[float] = field(default=0.0)
    repetition_penalty: Optional[float] = field(default=1.0)
    length_penalty: Optional[float] = field(default=1.0)
    no_repeat_ngram_size: Optional[int] = field(default=0)

def find_all_linear_names(args, model):
    cls = bnb.nn.Linear4bit if args.bits == 4 else (bnb.nn.Linear8bitLt if args.bits == 8 else torch.nn.Linear)
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, cls):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])
        # elif 'heads.' in name:
        #     names = name.split('.')
        #     lora_module_names.add(names[-1])
    
    if args.num_heads > 1:
        for i in range(args.num_heads):
            lora_module_names.add(f'heads.{i}')
    # if 'lm_head' in lora_module_names: # needed for 16-bit
    #     lora_module_names.remove('lm_head')
    return list(lora_module_names)


class SavePeftModelCallback(transformers.TrainerCallback):
    def save_model(self, args, state, kwargs):
        print('Saving PEFT checkpoint...')
        if state.best_model_checkpoint is not None:
            checkpoint_folder = os.path.join(state.best_model_checkpoint, "adapter_model")
        else:
            checkpoint_folder = os.path.join(args.output_dir, f"{PREFIX_CHECKPOINT_DIR}-{state.global_step}")

        peft_model_path = os.path.join(checkpoint_folder, "adapter_model")
        kwargs["model"].save_pretrained(peft_model_path)

        pytorch_model_path = os.path.join(checkpoint_folder, "pytorch_model.bin")
        if os.path.exists(pytorch_model_path):
            os.remove(pytorch_model_path)

    def on_save(self, args, state, control, **kwargs):
        self.save_model(args, state, kwargs)
        return control

    def on_train_end(self, args, state, control, **kwargs):
        def touch(fname, times=None):
            with open(fname, 'a'):
                os.utime(fname, times)

        touch(join(args.output_dir, 'completed'))
        self.save_model(args, state, kwargs)

def get_accelerate_model(args, checkpoint_dir):

    if torch.cuda.is_available():
        n_gpus = torch.cuda.device_count()
    else: n_gpus = 0
    if is_ipex_available() and torch.xpu.is_available():
        n_gpus = torch.xpu.device_count()
        
    max_memory = f'{args.max_memory_MB}MB'
    max_memory = {i: max_memory for i in range(n_gpus)}
    device_map = "auto"

    # if we are in a distributed setting, we need to set the device map and max memory per device
    if os.environ.get('LOCAL_RANK') is not None:
        local_rank = int(os.environ.get('LOCAL_RANK', '0'))
        device_map = {'': local_rank}
        max_memory = {'': max_memory[local_rank]}


    if args.full_finetune: assert args.bits in [16, 32]
    assert args.num_heads >= 1

    print(f'loading base model {args.model_name_or_path}...')
    compute_dtype = (torch.float16 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32))
    
    model = None
    config = None
    AutoModelsDict = {'causal': AutoModelForCausalLM, 'masked': AutoModelForMaskedLM}
    
    if args.num_heads > 1:
        config = MHLlamaConfig(**vars(args))
        # model = num_headsLlamaForCausalLM(args.num_heads, config)
        transformers.AutoConfig.register('mhllama', MHLlamaConfig)
        transformers.AutoModelForCausalLM.register(MHLlamaConfig, MultiheadLlamaForCausalLM)
        # model = AutoModelForCausalLM.from_pretrained(
        #     args.model_name_or_path,
        #     config = config,
        #     device_map=device_map,
        #     max_memory=max_memory,
        #     quantization_config=BitsAndBytesConfig(
        #         load_in_4bit=args.bits == 4,
        #         load_in_8bit=args.bits == 8,
        #         llm_int8_threshold=6.0,
        #         llm_int8_has_fp16_weight=False,
        #         bnb_4bit_compute_dtype=compute_dtype,
        #         bnb_4bit_use_double_quant=args.double_quant,
        #         bnb_4bit_quant_type=args.quant_type,
        #     ),
        #     torch_dtype=(torch.float32 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32)),
        # )
        
    model = AutoModelsDict[args.task].from_pretrained(
        args.model_name_or_path,
        config = config,
        device_map=device_map,
        max_memory=max_memory,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=args.bits == 4,
            load_in_8bit=args.bits == 8,
            llm_int8_threshold=6.0,
            llm_int8_has_fp16_weight=False,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=args.double_quant,
            bnb_4bit_quant_type=args.quant_type,
        ),
        torch_dtype=(torch.float32 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32)),
        trust_remote_code=args.trust_remote_code,
    )
        
    print(model)
        
    if compute_dtype == torch.float16 and args.bits == 4:
        if torch.cuda.is_bf16_supported():
            print('='*80)
            print('Your GPU supports bfloat16, you can accelerate training with the argument --bf16')
            print('='*80)
            
    if compute_dtype == torch.float16 and (is_ipex_available() and torch.xpu.is_available()):
        compute_dtype = torch.bfloat16
        print('Intel XPU does not support float16 yet, so switching to bfloat16')

    setattr(model, 'model_parallel', True)
    setattr(model, 'is_parallelizable', True)

    model.config.torch_dtype=(torch.float32 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32))

    # Tokenizer
    tok_path = args.model_name_or_path
    if args.num_heads > 1:
        tok_path = '/mnt/data/zoo/llama2/llama2-7b-hf/'
    tokenizer = AutoTokenizer.from_pretrained(
        tok_path,
        # cache_dir=args.cache_dir,
        padding_side="right",
        use_fast=False, # Fast tokenizer giving issues.
        tokenizer_type='llama' if 'llama' in args.model_name_or_path else None, # Needed for HF name change
        trust_remote_code=args.trust_remote_code,
        # use_auth_token=args.use_auth_token,
    )
    if tokenizer._pad_token is None:
        smart_tokenizer_and_embedding_resize(
            special_tokens_dict=dict(pad_token=DEFAULT_PAD_TOKEN),
            tokenizer=tokenizer,
            model=model,
        )
    if 'llama' in args.model_name_or_path or isinstance(tokenizer, LlamaTokenizer):
        # LLaMA tokenizer may not have correct special tokens set.
        # Check and add them if missing to prevent them from being parsed into different tokens.
        # Note that these are present in the vocabulary.
        # Note also that `model.config.pad_token_id` is 0 which corresponds to `<unk>` token.
        print('Adding special tokens.')
        tokenizer.add_special_tokens({
                "eos_token": tokenizer.convert_ids_to_tokens(model.config.eos_token_id),
                "bos_token": tokenizer.convert_ids_to_tokens(model.config.bos_token_id),
                "unk_token": tokenizer.convert_ids_to_tokens(
                    model.config.pad_token_id if model.config.pad_token_id != -1 else tokenizer.pad_token_id
                ),
        })
        if args.num_heads > 1:
            tokenizer.add_special_tokens({"cls_token": tokenizer.convert_ids_to_tokens(model.config.col_token_id)})
    
    if not args.full_finetune:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=args.gradient_checkpointing)

    if not args.full_finetune:
        if checkpoint_dir is not None:
            print("Loading adapters from checkpoint.")
            model = PeftModel.from_pretrained(model, join(checkpoint_dir, 'adapter_model'), is_trainable=True)
        else:
            print(f'adding LoRA modules...')
            modules = find_all_linear_names(args, model)
            print(modules)
            config = LoraConfig(
                r=args.lora_r,
                lora_alpha=args.lora_alpha,
                target_modules=modules,
                lora_dropout=args.lora_dropout,
                bias="none",
                task_type="CAUSAL_LM",
            )
            model = get_peft_model(model, config)

    for name, module in model.named_modules():
        if isinstance(module, LoraLayer):
            if args.bf16:
                module = module.to(torch.bfloat16)
        if 'norm' in name:
            module = module.to(torch.float32)
        if 'lm_head' in name or 'embed_tokens' in name:
            if hasattr(module, 'weight'):
                if args.bf16 and module.weight.dtype == torch.float32:
                    module = module.to(torch.bfloat16)

    return model, tokenizer

def print_trainable_parameters(args, model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    if args.bits == 4: trainable_params /= 2
    print(
        f"trainable params: {trainable_params} || "
        f"all params: {all_param} || "
        f"trainable: {100 * trainable_params / all_param}"
    )

def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: Dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))
    
    if num_new_tokens > 0:
        input_embeddings_data = model.get_input_embeddings().weight.data
        output_embeddings_data = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings_data[:-num_new_tokens].mean(dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings_data[:-num_new_tokens].mean(dim=0, keepdim=True)

        input_embeddings_data[-num_new_tokens:] = input_embeddings_avg
        output_embeddings_data[-num_new_tokens:] = output_embeddings_avg


@dataclass
class DataCollatorForMHLM:
    
    def __init__(self, tokenizer, source_max_len, target_max_len, train_on_source, predict_with_generate, 
                 prompt, vocab_masks, max_column_len):
        self.tokenizer = tokenizer
        self.source_max_len = source_max_len
        self.target_max_len = target_max_len
        self.predict_with_generate = predict_with_generate
        self.max_column_len = max_column_len
        
        print(prompt)
        self.num_cols = len(prompt)-1 #excludes prompt tokens/head
        for i in range(1, len(prompt)):
            prompt[i] = (f'{self.tokenizer.cls_token}' + prompt[i]).strip()
            
        prompt[0] = (str(self.tokenizer.bos_token) + str(prompt[0])).strip()
        prompt[-1] = str(prompt[-1]) + str(self.tokenizer.eos_token)
        # print(prompt)
        
        self.prompt_ids, _ = self.tokenizer(prompt,
            add_special_tokens=False).values() 
        self.prompt_ids = [torch.as_tensor(chunk) for chunk in self.prompt_ids] 
        
        self.prompt_head_inds = [torch.zeros_like(chunk) for chunk in self.prompt_ids]
        self.prompt_head_inds[0] = self.prompt_head_inds[0][1:] # removes BOS token, accounts for model shift all to left
        # print(self.prompt_ids)
        self.col_tok = self.tokenizer(f'{self.tokenizer.cls_token}', add_special_tokens=False)['input_ids'][0] #3
        
        self.vocab_masks = torch.as_tensor(pd.DataFrame(vocab_masks[:]).to_numpy().T, dtype=torch.bool) # num_cols x 32000 (or vocab size)
        
        
    def get_templates(self):
        # form head_inds
        col_head_inds = [be.squeeze() for be in torch.split(torch.arange(1,self.num_cols+1).unsqueeze(1).repeat(1,self.max_column_len),1)]
        head_inds = torch.cat( sum(([ae,be] for ae,be in zip(self.prompt_head_inds, col_head_inds)), []) +  [self.prompt_head_inds[-1]]) 
        #                                                      intersperse                               ^ since there's 1 more prompt, won't be in zip
        
        # for generation
        prompt_template = torch.zeros_like(head_inds, dtype=torch.long)
        prompt_template[torch.where(head_inds==0)] = torch.cat(self.prompt_ids, dim=0)[1:]
        return head_inds, prompt_template, self.vocab_masks, self.max_column_len
        
    
    def __call__(self, instances: Sequence[Dict]) -> Dict:
        # instances = instances[0]
        instances= pd.DataFrame(instances)
        # Extract elements
        batch_size = len(instances)
        if len(instances.columns) > 1: 
            include_labels = True
        else:
            include_labels = False
        instances = instances.drop('length', axis=1)
        
        if include_labels:
            # tokenize column labels
            targets = self.tokenizer(instances.values.reshape((1,-1))[0].tolist(),  # (num_cols*batch_size,) ndarray row1col1 row1col2 ... row2col1 row2col2 etc
                                    add_special_tokens=False, padding='max_length', return_tensors='pt', max_length=self.max_column_len, truncation=True)
            col_tokens_len = targets['input_ids'].shape[-1]
            # batch_size*num_cols x max_column_len,  pads tokens with 0s
            # print('targets', targets['input_ids'], targets['input_ids'].shape, self.max_column_len)
            
            # batch_size x num_cols x max_column_len:
            targets_tok = targets['input_ids'].reshape((batch_size, self.num_cols, self.max_column_len)) 
            # print('targets_tok', targets_tok.shape)
        
            # insert column labels into proper places within cloze prompt
            labels_list = [] 
            for i in range(self.num_cols):
                stretch_chunk = self.prompt_ids[i].repeat(batch_size, 1) # bs x tokens
                labels_list.extend([stretch_chunk, targets_tok[:,i,:]])
            stretch_chunk = self.prompt_ids[-1].repeat(batch_size, 1)
            labels_list.append(stretch_chunk)
            labels = torch.cat(labels_list, dim=1).long()
            # print(labels[0].shape, self.head_inds.shape,)
            # assert(labels.shape[1]-1 == self.head_inds.shape[0])
            
            input_ids = copy.deepcopy(labels)
            attention_mask = torch.ones_like(input_ids) # labels.ne(self.tokenizer.pad_token_id)
            data_dict = {
                'input_ids': input_ids,
                'attention_mask': attention_mask,
                'labels': labels
            }
            
        else: #not include labels
            input_ids = torch.tile(input=torch.as_tensor((self.tokenizer.bos_token_id, self.prompt_ids[0][1]), dtype=torch.long), 
                                   dims=(batch_size, 1))
            attention_mask = torch.ones_like(input_ids)
            data_dict = {
                'input_ids': input_ids,
                'attention_mask': attention_mask,
            }
        
        # print('prompt_ids', [c.shape for c in self.prompt_ids])
        # print('input_ids', input_ids.shape, input_ids)
        # print('labels   ', labels.shape, labels)
            
        return data_dict
    

@dataclass
class DataCollatorForCausalLM(object):
    tokenizer: transformers.PreTrainedTokenizer
    source_max_len: int
    target_max_len: int
    train_on_source: bool
    predict_with_generate: bool

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        # Extract elements
        sources = [f"{self.tokenizer.bos_token}{example['input']}" for example in instances]
        targets = [f"{example['output']}{self.tokenizer.eos_token}" for example in instances]
        # Tokenize
        tokenized_sources_with_prompt = self.tokenizer(
            sources,
            max_length=self.source_max_len,
            truncation=True,
            add_special_tokens=False,
        )
        tokenized_targets = self.tokenizer(
            targets,
            max_length=self.target_max_len,
            truncation=True,
            add_special_tokens=False,
        )
        # Build the input and labels for causal LM
        input_ids = []
        labels = []
        for tokenized_source, tokenized_target in zip(
            tokenized_sources_with_prompt['input_ids'],
            tokenized_targets['input_ids']
        ):
            if not self.predict_with_generate: # always here
                input_ids.append(torch.tensor(tokenized_source + tokenized_target))
                if not self.train_on_source: #always here
                    labels.append(
                        torch.tensor([IGNORE_INDEX for _ in range(len(tokenized_source))] + copy.deepcopy(tokenized_target))
                    )
                else:
                    labels.append(torch.tensor(copy.deepcopy(tokenized_source + tokenized_target)))
            else:
                input_ids.append(torch.tensor(tokenized_source))
        # Apply padding
        input_ids = pad_sequence(input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id)
        labels = pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX) if not self.predict_with_generate else None
        data_dict = {
            'input_ids': input_ids,
            'attention_mask':input_ids.ne(self.tokenizer.pad_token_id),
        }
        if labels is not None:
            data_dict['labels'] = labels
        return data_dict

def extract_unnatural_instructions_data(examples, extract_reformulations=False):
    out = {
        'input': [],
        'output': [],
    }
    for example_instances in examples['instances']:
        for instance in example_instances:
            out['input'].append(instance['instruction_with_input'])
            out['output'].append(instance['output'])
    if extract_reformulations:
        for example_reformulations in examples['reformulations']:
            if example_reformulations is not None:
                for instance in example_reformulations:
                    out['input'].append(instance['instruction_with_input'])
                    out['output'].append(instance['output'])
    return out

ALPACA_PROMPT_DICT = {
    "prompt_input": (
        "Below is an instruction that describes a task, paired with an input that provides further context. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response: "
    ),
    "prompt_no_input": (
        "Below is an instruction that describes a task. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Response: "
    ),
}

def extract_alpaca_dataset(example):
    if example.get("input", "") != "":
        prompt_format = ALPACA_PROMPT_DICT["prompt_input"]
    else:
        prompt_format = ALPACA_PROMPT_DICT["prompt_no_input"]
    return {'input': prompt_format.format(**example)}

def local_dataset(dataset_name):
    if dataset_name.endswith('.json') or dataset_name.endswith('.jsonl'):
        full_dataset = Dataset.from_json(path_or_paths=dataset_name)
    elif dataset_name.endswith('.csv'):
        full_dataset = Dataset.from_pandas(pd.read_csv(dataset_name))
    elif dataset_name.endswith('.tsv'):
        full_dataset = Dataset.from_pandas(pd.read_csv(dataset_name, delimiter='\t'))
    elif dataset_name.endswith('.dat'):
        if 'dataset_dict.json' in os.listdir(dataset_name):
            full_dataset = DatasetDict({})
            for f in os.listdir(dataset_name):
                if f.endswith('.json'): continue
                full_dataset[f] = load_from_disk(os.path.join(dataset_name, f))
            return full_dataset
        else:
            full_dataset = load_from_disk(dataset_name)
    else:
        raise ValueError(f"Unsupported dataset format: {dataset_name}")

    split_dataset = full_dataset.train_test_split(test_size=0.1)
    return split_dataset

def make_data_module(tokenizer: transformers.PreTrainedTokenizer, args) -> Dict:
    """
    Make dataset and collator for supervised fine-tuning.
    Datasets are expected to have the following columns: { `input`, `output` }
    """
    def load_data(dataset_name):
        # if dataset_name == 'alpaca':
        #     return load_dataset("tatsu-lab/alpaca")
        if os.path.exists(dataset_name):
            return local_dataset(dataset_name)
        else:
            raise NotImplementedError(f"Dataset {dataset_name} not implemented yet.")

    def format_dataset(dataset, dataset_format):
        if dataset_format == 'inout': #input and output columns
            # Remove unused columns.
            dataset = dataset.remove_columns(
                [col for col in dataset.column_names['train'] if col not in ['input', 'output']]
            )
        elif dataset_format == 'mlm':
            # find relevant columns and remove others
            pass
        return dataset

     # Load dataset.
    dataset = load_data(args.dataset)
    dataset = format_dataset(dataset, args.dataset_format)

    # Split train/eval, reduce size
    if args.do_eval or args.do_predict:
        if 'eval' in dataset:
            eval_dataset = dataset['eval']
        elif args.eval_dataset_size > 0:
            print('Splitting train dataset in train and validation according to `eval_dataset_size`')
            dataset = dataset["train"].train_test_split(
                test_size=args.eval_dataset_size, shuffle=True, seed=42
            )
            eval_dataset = dataset['test']
        if args.max_eval_samples is not None and args.eval_dataset_size>0 and len(eval_dataset) > args.max_eval_samples:
            eval_dataset = eval_dataset.select(range(args.max_eval_samples))
        # if args.group_by_length and args.eval_dataset_size>0:
        #     eval_dataset = eval_dataset.map(lambda x: {'length': sum([len(x[col] for col in x)])})
    if args.do_train or args.do_generate:
        train_dataset = dataset['train']
        if args.do_train and args.max_train_samples is not None and len(train_dataset) > args.max_train_samples:
            train_dataset = train_dataset.select(range(args.max_train_samples))
        # if args.group_by_length:
        #     train_dataset = train_dataset.map(lambda x: {'length': len(x['input']) + len(x['output'])})

    if 'prompt' in dataset:
        data_collator = DataCollatorForMHLM(
            tokenizer=tokenizer,
            source_max_len=args.source_max_len,
            target_max_len=args.target_max_len,
            train_on_source=args.train_on_source,
            predict_with_generate=args.predict_with_generate,
            prompt = dataset['prompt']['prompt'],
            vocab_masks = dataset['vocab_masks'], # {'1':32000-list, '2':32000-list, etc}
            max_column_len = args.generation_config.max_column_len,
        )
    else:
        data_collator = DataCollatorForCausalLM(
            tokenizer=tokenizer,
            source_max_len=args.source_max_len,
            target_max_len=args.target_max_len,
            train_on_source=args.train_on_source,
            predict_with_generate=args.predict_with_generate,
        )
    return dict(
        train_dataset=train_dataset if args.do_train or args.do_generate else None,
        eval_dataset=eval_dataset if args.do_eval else None,
        predict_dataset=eval_dataset if args.do_predict else None,
        data_collator=data_collator
    )

def get_last_checkpoint(checkpoint_dir):
    if isdir(checkpoint_dir):
        is_completed = exists(join(checkpoint_dir, 'completed'))
        # if is_completed: return None, True # already finished
        max_step = 0
        for filename in os.listdir(checkpoint_dir):
            if isdir(join(checkpoint_dir, filename)) and filename.startswith('checkpoint'):
                max_step = max(max_step, int(filename.replace('checkpoint-', '')))
        if max_step == 0: return None, is_completed # training started, but no checkpoint
        checkpoint_dir = join(checkpoint_dir, f'checkpoint-{max_step}')
        print(f"Found a previous checkpoint at: {checkpoint_dir}")
        return checkpoint_dir, is_completed # checkpoint found!
    return None, False # first training

def sample(args):
    dataname = args.dataset.split('/')[-2]
    modelname = args.output_dir.split('/')[-1]
    path = f'/mnt/data/sonia/datasets/synthetic/{dataname}/{modelname}.dat'
    
    tokenizer = AutoTokenizer.from_pretrained('/mnt/data/zoo/llama2/llama2-7b-hf/',
            padding_side="right",
            use_fast=False, # Fast tokenizer giving issues.
            )
    data_module = make_data_module(tokenizer=tokenizer, args=args)
    collator = data_module['data_collator']
    real = data_module['train_dataset'].to_pandas().drop(['length'], axis=1)
    tokenizer = collator.tokenizer
    
    ckpt_path = get_last_checkpoint(args.output_dir)[0]
    transformers.AutoConfig.register('mhllama', MHLlamaConfig)
    transformers.AutoModelForCausalLM.register(MHLlamaConfig, MultiheadLlamaForCausalLM)
    config = MHLlamaConfig(**vars(args))
    print('loading from', ckpt_path)
    model = AutoModelForCausalLM.from_pretrained(
                ckpt_path,
                config = config, device_map='cpu')
    model.set_templates(collator.get_templates())
    model = PeftModel.from_pretrained(model, join(ckpt_path, 'adapter_model'), is_trainable=True)
    model = model.merge_and_unload()
    
    preds = [ [] for _ in range(real.shape[1]) ]
    batch_size = 100
    num_samples = real.shape[0]
    inputs = collator(batch_size*[{'length': 0}])
    
    print('beginning generation')

    for batch in tqdm(range(num_samples//batch_size + 1)):
        _, batch_col_toks = model.generate(**inputs) # batch_size x num_cols x max_column_len

        for i, col in enumerate(real.columns):
            options_str = real[col].unique()
            options = tokenizer(options_str.tolist(), add_special_tokens=False, padding='max_length', return_tensors='pt', 
                                max_length=args.generation_config.max_column_len, truncation=True)['input_ids']
            preds_col = options_str[cosine_similarity(batch_col_toks[:, i, :], options).argmax(axis=1)]
            preds[i].extend(preds_col)
            
            hp = Dataset.from_pandas(pd.DataFrame(preds).T)
            hp.save_to_disk(path)


def train():
    hfparser = transformers.HfArgumentParser((
        ModelArguments, DataArguments, TrainingArguments, GenerationArguments
    ))
    model_args, data_args, training_args, generation_args   , extra_args = \
        hfparser.parse_args_into_dataclasses(return_remaining_strings=True)
    training_args.generation_config = transformers.GenerationConfig(**vars(generation_args))
    args = argparse.Namespace(
        **vars(model_args), **vars(data_args), **vars(training_args)
    )
    # print(args)
    os.environ["WANDB_PROJECT"]="mhllama"
    os.environ['WANDB_RUN_ID']=args.output_dir.split('/')[-1] + datetime.now().strftime('-%m.%d-%H.%M')
    
    checkpoint_dir, completed_training = get_last_checkpoint(args.output_dir)
    if completed_training:
        print('Detected that training was already completed!')

    if args.do_train or args.do_eval or args.do_predict:
        model, tokenizer = get_accelerate_model(args, checkpoint_dir)

        model.config.use_cache = False
        print('loaded model')
        set_seed(args.seed)

        data_module = make_data_module(tokenizer=tokenizer, args=args)
        
        if args.num_heads > 1:
            model.set_templates(data_module['data_collator'].get_templates()) # head_inds and prompt_template
            do_mlm_sample=True # controls whether to do multihead-style sampling, will be passed as generation arg
        
        # if args.diversity:
        #     class CustomSeq2SeqTrainer(Seq2SeqTrainer):
        #         def compute_loss(self, model, inputs, return_outputs=False):
        #             """
        #             How the loss is computed by Trainer. By default, all models return the loss in the first element.

        #             Subclass and override for custom behavior.
        #             """
        #             if self.label_smoother is not None and "labels" in inputs:
        #                 labels = inputs.pop("labels")
        #             else:
        #                 labels = None
        #             outputs = model(**inputs)
                    
        #             # Save past state if it exists
        #             # TODO: this needs to be fixed and made cleaner later.
        #             if self.args.past_index >= 0:
        #                 self._past = outputs[self.args.past_index]

        #             if labels is not None:
        #                 unwrapped_model = unwrap_model(model)
        #                 if is_peft_available() and isinstance(unwrapped_model, PeftModel):
        #                     model_name = unwrapped_model.base_model.model._get_name()
        #                 else:
        #                     model_name = unwrapped_model._get_name()
        #                 if model_name in MODEL_FOR_CAUSAL_LM_MAPPING_NAMES.values():
        #                     loss = self.label_smoother(outputs, labels, shift_labels=True)
        #                 else:
        #                     loss = self.label_smoother(outputs, labels)
        #             else:
        #                 if isinstance(outputs, dict) and "loss" not in outputs:
        #                     raise ValueError(
        #                         "The model did not return a loss from the inputs, only the following keys: "
        #                         f"{','.join(outputs.keys())}. For reference, the inputs it received are {','.join(inputs.keys())}."
        #                     )
        #                 # We don't use .loss here since the model may return tuples instead of ModelOutput.
        #                 loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]
                        
        #             # Diversity term
        #             if args.divdist == 'manhattan':
        #                 dist_matrix = manhattan(outputs.logits.squeeze())
        #             elif args.divdist == 'cosine':
        #                 dist_matrix = cossim(outputs.logits.squeeze())
        #             else:
        #                 return ValueError(f'Unsupported diversity distance function {args.divdist}')
        #             dist_matrix = dist_matrix.to(outputs.logits.get_device()) / args.divc1
        #             diversity = torch.mean(torch.exp(-dist_matrix)) * args.divc2
        #             print(diversity)

        #             return (loss+diversity, outputs) if return_outputs else loss+diversity
            
        #     trainerclass = CustomSeq2SeqTrainer
        # else: 
        trainerclass = Seq2SeqTrainer
                    
        
        trainer = trainerclass(
            model=model,
            tokenizer=tokenizer,
            args=training_args,
            **{k:v for k,v in data_module.items() if k != 'predict_dataset'},
        )

        # Callbacks
        if not args.full_finetune:
            trainer.add_callback(SavePeftModelCallback)
        if args.report_to == ['wandb']:
            print('adding wandb callback')
            class WandbMetricsCallback(WandbCallback):
                def on_substep_end(self, args, state, control, **kwargs):
                    self._wandb.log(model.to_log)
                def on_prediction_step(self, args, state, control, **kwargs):
                    mets = {k+'_eval':model.to_log[k] for k in model.to_log}
                    self._wandb.log(model.to_log)
                    
            trainer.add_callback(WandbMetricsCallback)
        if args.eval_samples:
            class evalSampleCallback(transformers.TrainerCallback):
                def on_evaluate(self, args, state, control, model, **kwargs):
                    trainer.model.eval()
                    metrics = trainer.predict(test_dataset=data_module['eval_dataset'],metric_key_prefix="predict")
                    
                    predictions = []
                    for i in range(len(metrics.predictions)):
                        logit = metrics.predictions[i]
                        # print('logit', logit)
                        # print(logit.shape)
                        label = metrics.label_ids[i] #just to see positions where prompt tokens are at
                        # print('label', label)
                        # print(label.shape)
                        logit_abcd = logit[label != IGNORE_INDEX]
                        toks = np.argmax(logit_abcd, axis=1)
                        predictions.append(
                            ''.join(trainer.tokenizer.decode(toks, skip_special_tokens=True, clean_up_tokenization_spaces=True)) + '\n'
                            )
                    
                    with open(os.path.join(args.output_dir, 'samples.txt'), 'a') as f:
                        f.write(f'step {trainer.state.global_step}\n')
                        f.writelines(predictions)
                        f.write('\n\n')
                        
                    print('\nsamples logged to ', os.path.join(args.output_dir, 'samples.txt'))
                    
            trainer.add_callback(evalSampleCallback)
        if args.do_mmlu_eval:
            if args.mmlu_dataset == 'mmlu-zs':
                mmlu_dataset = load_dataset("json", data_files={
                    'eval': 'data/mmlu/zero_shot_mmlu_val.json',
                    'test': 'data/mmlu/zero_shot_mmlu_test.json',
                })
                mmlu_dataset = mmlu_dataset.remove_columns('subject')
            # MMLU Five-shot (Eval/Test only)
            elif args.mmlu_dataset == 'mmlu' or args.mmlu_dataset == 'mmlu-fs':
                mmlu_dataset = load_dataset("json", data_files={
                    'eval': 'data/mmlu/five_shot_mmlu_val.json',
                    'test': 'data/mmlu/five_shot_mmlu_test.json',
                })
                # mmlu_dataset = mmlu_dataset.remove_columns('subject')
            mmlu_dataset = mmlu_dataset[args.mmlu_split]
            if args.max_mmlu_samples is not None:
                mmlu_dataset = mmlu_dataset.select(range(args.max_mmlu_samples))
            abcd_idx = [
                tokenizer("A", add_special_tokens=False).input_ids[0],
                tokenizer("B", add_special_tokens=False).input_ids[0],
                tokenizer("C", add_special_tokens=False).input_ids[0],
                tokenizer("D", add_special_tokens=False).input_ids[0],
            ]
            accuracy = evaluate.load("accuracy")
            class MMLUEvalCallback(transformers.TrainerCallback):
                def on_evaluate(self, args, state, control, model, **kwargs):
                    data_loader = trainer.get_eval_dataloader(mmlu_dataset)
                    source_max_len = trainer.data_collator.source_max_len
                    trainer.data_collator.source_max_len = args.mmlu_source_max_len
                    trainer.model.eval()
                    preds, refs = [], []
                    loss_mmlu = 0
                    for batch in tqdm(data_loader, total=len(data_loader)):
                        (loss, logits, labels) = trainer.prediction_step(trainer.model,batch,prediction_loss_only=False,)
                        # There are two tokens, the output, and eos token.
                        for i, logit in enumerate(logits):
                            label_non_zero_id = (batch['labels'][i] != -100).nonzero()[0][0]
                            logit_abcd = logit[label_non_zero_id-1][abcd_idx]
                            preds.append(torch.argmax(logit_abcd).item())
                        labels = labels[labels != IGNORE_INDEX].view(-1, 2)[:,0]
                        refs += [abcd_idx.index(label) for label in labels.tolist()]
                        loss_mmlu += loss.item()
                    # Extract results by subject.
                    results = {'mmlu_loss':loss_mmlu/len(data_loader)}
                    subject = mmlu_dataset['subject']
                    subjects = {s:{'refs':[], 'preds':[]} for s in set(subject)}
                    for s,p,r in zip(subject, preds, refs):
                        subjects[s]['preds'].append(p)
                        subjects[s]['refs'].append(r)
                    subject_scores = []
                    for subject in subjects:
                        subject_score = accuracy.compute(
                            references=subjects[subject]['refs'],
                            predictions=subjects[subject]['preds']
                        )['accuracy']
                        results[f'mmlu_{args.mmlu_split}_accuracy_{subject}'] = subject_score
                        subject_scores.append(subject_score)
                    results[f'mmlu_{args.mmlu_split}_accuracy'] = np.mean(subject_scores)
                    trainer.log(results)
                    trainer.data_collator.source_max_len = source_max_len

            trainer.add_callback(MMLUEvalCallback)

        # Verifying the datatypes and parameter counts before training.
        print_trainable_parameters(args, model)
        dtypes = {}
        for _, p in model.named_parameters():
            dtype = p.dtype
            if dtype not in dtypes: dtypes[dtype] = 0
            dtypes[dtype] += p.numel()
        total = 0
        for k, v in dtypes.items(): total+= v
        for k, v in dtypes.items():
            print(k, v, v/total)

        all_metrics = {"run_name": args.run_name}
        # Training
        if args.do_train:
            logger.info("*** Train ***")
            # Note: `resume_from_checkpoint` not supported for adapter checkpoints by HF.
            # Currently adapter checkpoint is reloaded as expected but optimizer/scheduler states are not.
            train_result = trainer.train()
            metrics = train_result.metrics
            trainer.log_metrics("train", metrics)
            trainer.save_metrics("train", metrics)
            trainer.save_state()
            all_metrics.update(metrics)
        # Evaluation
        if args.do_eval:
            logger.info("*** Evaluate ***")
            metrics = trainer.evaluate(metric_key_prefix="eval")
            trainer.log_metrics("eval", metrics)
            trainer.save_metrics("eval", metrics)
            all_metrics.update(metrics)
        # Prediction
        if args.do_predict:
            logger.info("*** Predict ***")
            prediction_output = trainer.predict(test_dataset=data_module['predict_dataset'],metric_key_prefix="predict")
            prediction_metrics = prediction_output.metrics
            predictions = prediction_output.predictions
            predictions = np.where(predictions != -100, predictions, tokenizer.pad_token_id)
            predictions = tokenizer.batch_decode(
                predictions, skip_special_tokens=True, clean_up_tokenization_spaces=True
            )
            with open(os.path.join(args.output_dir, 'predictions.jsonl'), 'w') as fout:
                for i, example in enumerate(data_module['predict_dataset']):
                    example['prediction_with_input'] = predictions[i].strip()
                    example['prediction'] = predictions[i].replace(example['input'], '').strip()
                    fout.write(json.dumps(example) + '\n')
            print(prediction_metrics)
            trainer.log_metrics("predict", prediction_metrics)
            trainer.save_metrics("predict", prediction_metrics)
            all_metrics.update(prediction_metrics)


    # if args.do_generate:
    #     sample(args)

    if (args.do_train or args.do_eval or args.do_predict):
        with open(os.path.join(args.output_dir, "metrics.json"), "w") as fout:
            fout.write(json.dumps(all_metrics))

if __name__ == "__main__":
    train()
