# coding:utf-8
# Copyright (c) 2021  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
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

import glob
import json
import math
import os
import copy
import itertools

import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from ..datasets import MapDataset
from ..data import Stack, Pad, Tuple, Vocab, JiebaTokenizer
from .utils import download_file
from .model import BoWModel, LSTMModel
from .task import Task

URLS = {
    "senta_vocab":
    ["https://paddlenlp.bj.bcebos.com/data/senta_word_dict.txt", None],
    "senta_bow": [
        "https://paddlenlp.bj.bcebos.com/taskflow/sentiment_analysis/senta/senta_bow.pdparams",
        None
    ],
    "senta_lstm": [
        "https://paddlenlp.bj.bcebos.com/taskflow/sentiment_analysis/senta/senta_lstm.pdparams",
        None
    ]
}


class SentaTask(Task):
    """The one task of sentiment_analysis which use the RNN or Bow model to analysis the input text. 
    """

    def __init__(self, task, model, **kwargs):
        super().__init__(task=task, model=model, **kwargs)
        self._tokenizer = self._construct_tokenizer(model)
        self._model_instance = self._construct_model(model)
        self._label_map = {0: 'negative', 1: 'positive'}

    def _construct_model(self, model):
        """Construct the inference model for the predictor.
        """
        vocab_size = self.kwargs['vocab_size']
        pad_token_id = self.kwargs['pad_token_id']
        num_classes = 2

        # Select the senta network for the inference
        network = "bow"
        if 'network' in self.kwargs:
            network = self.kwargs['network']
        if network == "bow":
            model = BoWModel(vocab_size, num_classes, padding_idx=pad_token_id)
            model_full_name = download_file(self.model, "senta_bow.pdparams",
                                            URLS['senta_bow'][0],
                                            URLS['senta_bow'][1])
        elif network == "lstm":
            model = LSTMModel(
                vocab_size,
                num_classes,
                direction='forward',
                padding_idx=pad_token_id,
                pooling_type='max')
            model_full_name = download_file(self.model, "senta_lstm.pdparams",
                                            URLS['senta_lstm'][0],
                                            URLS['senta_lstm'][1])
        else:
            raise ValueError(
                "Unknown network: {}, it must be one of bow, lstm.".format(
                    network))

        # Load the model parameter for the predict
        state_dict = paddle.load(model_full_name)
        model.set_dict(state_dict)
        return model

    def _construct_tokenizer(self, model):
        """Construct the tokenizer for the predictor.
        """
        full_name = download_file(self.model, "senta_word_dict.txt",
                                  URLS['senta_vocab'][0],
                                  URLS['senta_vocab'][1])
        vocab = Vocab.load_vocabulary(
            full_name, unk_token='[UNK]', pad_token='[PAD]')

        vocab_size = len(vocab)
        pad_token_id = vocab.to_indices('[PAD]')
        # Construct the tokenizer form the JiebaToeknizer
        self.kwargs['pad_token_id'] = pad_token_id
        self.kwargs['vocab_size'] = vocab_size
        tokenizer = JiebaTokenizer(vocab)
        return tokenizer

    def _preprocess(self, inputs, padding=True, add_special_tokens=True):
        """
        Transform the raw text to the model inputs, two steps involved:
           1) Transform the raw text to token ids.
           2) Generate the other model inputs from the raw text and token ids.
        """
        inputs = inputs[0]
        if isinstance(inputs, str):
            inputs = [inputs]
        if not isinstance(inputs, str) and not isinstance(inputs, list):
            raise TypeError(
                "Invalid inputs, input text should be str or list of str, {type(inputs)} found!"
            )
        infer_data = []
        for i in range(0, len(inputs)):
            ids = self._tokenizer.encode(inputs[i])
            lens = len(ids)
            infer_data.append([ids, lens])
        infer_ds = MapDataset(infer_data)
        batchify_fn = lambda samples, fn=Tuple(
            Pad(axis=0, pad_val=self._tokenizer.vocab.token_to_idx.get('[PAD]', 0)),  # input_ids
            Stack(dtype='int64'),  # seq_len
        ): fn(samples)

        batch_size = self.kwargs[
            'batch_size'] if 'batch_size' in self.kwargs else 1
        num_workers = self.kwargs[
            'num_workers'] if 'num_workers' in self.kwargs else 0
        infer_data_loader = paddle.io.DataLoader(
            infer_ds,
            collate_fn=batchify_fn,
            num_workers=num_workers,
            batch_size=batch_size,
            shuffle=False,
            return_list=True)
        outputs = {}
        outputs['text'] = inputs
        outputs['data_loader'] = infer_data_loader
        return outputs

    def _run_model(self, inputs):
        """Run the task model from the outputs of the `_tokenize` function. 
        """
        results = []
        with paddle.no_grad():
            for batch in inputs['data_loader']:
                input_ids, seq_len = batch
                logits = self._model_instance(input_ids, seq_len)
                probs = F.softmax(logits, axis=1)
                idx = paddle.argmax(probs, axis=1).numpy()
                idx = idx.tolist()
                labels = [self._label_map[i] for i in idx]
                results.extend(labels)
        inputs['result'] = results
        return inputs

    def _postprocess(self, inputs):
        """The model output is allways the logits and pros, this function will convert the model output to raw text.
        """
        final_results = []
        for text, label in zip(inputs['text'], inputs['result']):
            result = {}
            result['text'] = text
            result['label'] = label
            final_results.append(result)
        return final_results
