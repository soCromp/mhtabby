import os
from qlora import *
from collections import defaultdict
import copy
import json
from os.path import exists, join, isdir
from dataclasses import dataclass, field
import sys
from typing import Optional, Dict, Sequence
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
import numpy as np

import torch
import transformers
from torch.nn.utils.rnn import pad_sequence
import argparse
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    set_seed,
    Seq2SeqTrainer,
    BitsAndBytesConfig,
    LlamaTokenizer

)
from datasets import load_dataset, Dataset, load_from_disk
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

from sklearn.metrics.pairwise import cosine_similarity

path = '/mnt/data/sonia/datasets/synthetic/adult/may10-2.dat'
argdict = {
  'model_name_or_path' : '/mnt/data/sonia/ckpts/adult-good/checkpoint-60', # './mhllama',
  'num_heads': 7,
  'max_column_len': 5,
  'data_seed' : 42 ,
  'per_device_eval_batch_size' : 1 ,
  'dataloader_num_workers' : 1 ,
  'bf16' : True,
  'bits' : 4 ,
  'dataset' : '/mnt/data/sonia/datasets/adult/may8.dat',
  'seed' : 0,
  'max_new_tokens': 500,
}

arglist = [f'--{k}={v}' for k,v in argdict.items()]

hfparser = transformers.HfArgumentParser((
    ModelArguments, DataArguments, TrainingArguments, GenerationArguments
))
model_args, data_args, training_args, generation_args  = hfparser.parse_args_into_dataclasses(args=arglist, return_remaining_strings=True)[:-1]
training_args.generation_config = transformers.GenerationConfig(**vars(generation_args))
args = argparse.Namespace(
    **vars(model_args), **vars(data_args), **vars(training_args)
)

print('parsed args')

tokenizer = AutoTokenizer.from_pretrained('/mnt/data/zoo/llama2/llama2-7b-hf/',
        padding_side="right",
        use_fast=False, # Fast tokenizer giving issues.
        )
data_module = make_data_module(tokenizer=tokenizer, args=args)
collator = data_module['data_collator']
print('data loaded')

transformers.AutoConfig.register('mhllama', MHLlamaConfig)
transformers.AutoModelForCausalLM.register(MHLlamaConfig, MultiheadLlamaForCausalLM)

config = MHLlamaConfig(**vars(args))
model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            config = config,)
print('model loaded')

model.set_templates(collator.get_templates())
model = PeftModel.from_pretrained(model, join(args.model_name_or_path, 'adapter_model'), is_trainable=True)
model = model.merge_and_unload().cuda()
print('peft model unloaded')

full_dataset = DatasetDict({})
for f in os.listdir(args.dataset):
    if f.endswith('.json'): continue
    full_dataset[f] = load_from_disk(os.path.join(args.dataset, f))
real = full_dataset['train'].to_pandas().drop(['length'], axis=1)

preds = [ [] for _ in range(real.shape[1]) ]
batch_size = 100
num_samples = real.shape[0]
inputs = collator(batch_size*[{'length': 0}])
inputs={inputs[c].cuda() for c in inputs}

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