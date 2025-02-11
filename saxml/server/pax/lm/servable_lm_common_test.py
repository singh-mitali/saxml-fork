# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for servable_lm_common."""

import os

from absl import flags
import numpy as np
from praxis import pax_fiddle
from praxis import py_utils
from praxis import sample_decode
from praxis import test_utils
from saxml.server.jax import np_tf_sess_wrapper
from saxml.server.pax.lm import lm_tokenizer
from saxml.server.pax.lm import servable_lm_common
import tensorflow as tf

FLAGS = flags.FLAGS


def create_tokenizer_params():
  p = pax_fiddle.Config(
      lm_tokenizer.LMTokenizer,
      spm_model=os.path.join(
          FLAGS.test_srcdir,
          '__main__/saxml/server/pax/lm/test_data',
          'test_model.model',
      ),
      slice_left=False,
      target_sos_id=0,
      target_eos_id=1,
  )
  return p


class ServableLmCommonTest(tf.test.TestCase, test_utils.TestCase):

  def setUp(self):
    super().setUp()
    self.tokenizer = create_tokenizer_params().Instantiate()

  @test_utils.parameterized.parameters((True,), (False,))
  def test_decode_post_processing_decoder_only(self, use_wrapper: bool):
    max_length = 8
    strs = ['Hello world', 'This is a test']
    tokens = [
        [
            b'\xe2\x96\x81He',
            b'll',
            b'o',
            b'\xe2\x96\x81world',
            b'<unk>',
            b'<unk>',
            b'<unk>',
        ],
        [
            b'\xe2\x96\x81This',
            b'\xe2\x96\x81is',
            b'\xe2\x96\x81a',
            b'\xe2\x96\x81',
            b't',
            b'est',
            b'<unk>',
        ],
    ]
    ids, _, paddings = self.tokenizer.StringsToIds(strs, max_length)
    # This SPM doesn't support padding, decoding 0 to <unk>, so trim the output.
    ids = tf.slice(ids, [0, 1], [-1, -1])
    output_ids = tf.expand_dims(ids, 0).numpy()
    top_candidate_ids = np.expand_dims(output_ids, -1)
    padded_top_candidate_ids = np.pad(
        top_candidate_ids,
        (
            (0, 0),
            (0, 0),
            (0, 0),
            (0, sample_decode.MAX_NUM_PER_TOKEN_LOGPROBS - 1),
        ),
    )
    sampled_logprobs = np.ones_like(output_ids)
    top_candidate_logprobs = np.ones_like(top_candidate_ids)
    padded_top_candidate_logprobs = np.ones_like(padded_top_candidate_ids)
    paddings = tf.slice(paddings, [0, 1], [-1, -1])
    computed_outputs = py_utils.NestedMap(
        # First sequence has last dim padded.
        output_ids=output_ids,
        # Non-paddings as decoded length for each tensor.
        decode_lengths=tf.expand_dims(
            tf.math.reduce_sum(
                tf.cast(tf.equal(paddings, 0), tf.int32), axis=-1
            ),
            0,
        ).numpy(),
        scores=np.asarray([[[0.1], [0.2]]]),
        logprobs=sampled_logprobs,
        num_per_token_logprobs=np.array([[1], [1]]),
        top_candidate_ids=padded_top_candidate_ids,
        top_candidate_logprobs=padded_top_candidate_logprobs,
    )

    def decode_tf_post_processing(*args):
      return servable_lm_common.decode_tf_post_processing(
          *args,
          tokenizer=self.tokenizer,
          t5_model=False,
          include_prefix_in_result=True,
      )

    if use_wrapper:
      decode_tf_post_processing = np_tf_sess_wrapper.wrap_tf_session(
          decode_tf_post_processing
      )

    out = decode_tf_post_processing(computed_outputs)

    self.assertContainsExactSubsequence(
        [s.numpy().decode() for s in tf.squeeze(out['topk_decoded'])], strs
    )
    self.assertArraysEqual(
        np.asarray([4, 6]), tf.squeeze(out['topk_decode_lengths']).numpy()
    )

    top_candidate_tokens_per_step = out['top_candidate_tokens_per_step']
    self.assertEqual(
        top_candidate_tokens_per_step.shape,
        (1, 2, 7, sample_decode.MAX_NUM_PER_TOKEN_LOGPROBS),
    )
    top_candidate_tokens_per_step = tf.squeeze(
        top_candidate_tokens_per_step[:, :, :, 0]
    ).numpy()
    self.assertArraysEqual(
        top_candidate_tokens_per_step, tokens, check_dtypes=False
    )

    top_candidate_logprobs_per_step = out['top_candidate_logprobs_per_step']
    self.assertEqual(
        top_candidate_logprobs_per_step.shape,
        (1, 2, 7, sample_decode.MAX_NUM_PER_TOKEN_LOGPROBS),
    )
    top_candidate_logprobs_per_step = tf.expand_dims(
        top_candidate_logprobs_per_step[:, :, :, 0], -1).numpy()
    self.assertArraysEqual(
        top_candidate_logprobs_per_step, top_candidate_logprobs)

    sampled_tokens_per_step = out['sampled_tokens_per_step']
    self.assertEqual(
        sampled_tokens_per_step.shape,
        (1, 2, 7)
    )
    sampled_tokens_per_step = tf.squeeze(
        sampled_tokens_per_step).numpy()
    self.assertArraysEqual(sampled_tokens_per_step, tokens,
                           check_dtypes=False)

    self.assertArraysEqual(out['sampled_logprobs_per_step'],
                           sampled_logprobs)

  @test_utils.parameterized.named_parameters(
      [('None batch_size', None), ('With batch_size', 2)]
  )
  def test_extra_inputs_to_tf_signature(self, batch_size):
    default_extra_inputs = {
        'a': tf.zeros(3, dtype=tf.float32),
        'b': tf.ones(5, dtype=tf.float32),
    }
    extra_inputs_dtypes = {
        'a': tf.float32,
        'b': tf.float32,
    }
    extra_tensor_specs = servable_lm_common.extra_inputs_to_tf_signature(
        default_extra_inputs, batch_size, extra_inputs_dtypes
    )

    default_val_a = tf.zeros([batch_size or 1, 3], dtype=tf.float32)
    default_val_b = tf.ones([batch_size or 1, 5], dtype=tf.float32)
    self.assertArraysEqual(extra_tensor_specs['a'].default_val, default_val_a)
    self.assertArraysEqual(extra_tensor_specs['b'].default_val, default_val_b)


if __name__ == '__main__':
  tf.test.main()
