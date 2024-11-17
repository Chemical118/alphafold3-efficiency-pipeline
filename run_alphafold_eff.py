# Copyright 2024 DeepMind Technologies Limited
#
# AlphaFold 3 source code is licensed under CC BY-NC-SA 4.0. To view a copy of
# this license, visit https://creativecommons.org/licenses/by-nc-sa/4.0/
#
# To request access to the AlphaFold 3 model parameters, follow the process set
# out at https://github.com/google-deepmind/alphafold3. You may only use these
# if received directly from Google. Use is subject to terms of use available at
# https://github.com/google-deepmind/alphafold3/blob/main/WEIGHTS_TERMS_OF_USE.md

"""AlphaFold 3 structure prediction script.

AlphaFold 3 source code is licensed under CC BY-NC-SA 4.0. To view a copy of
this license, visit https://creativecommons.org/licenses/by-nc-sa/4.0/

To request access to the AlphaFold 3 model parameters, follow the process set
out at https://github.com/google-deepmind/alphafold3. You may only use these
if received directly from Google. Use is subject to terms of use available at
https://github.com/google-deepmind/alphafold3/blob/main/WEIGHTS_TERMS_OF_USE.md
"""

from collections.abc import Callable, Iterable, Sequence
import csv
import dataclasses
import functools
import multiprocessing
import os
import pathlib
import shutil
import string
import textwrap
import time
import typing
from typing import Protocol, Self, TypeVar, overload


from absl import app
from absl import flags
from alphafold3.common import base_config
from alphafold3.common import folding_input
from alphafold3.common import resources
from alphafold3.constants import chemical_components
import alphafold3.cpp
from alphafold3.data import featurisation
from alphafold3.data import pipeline_eff as pipeline
from alphafold3.jax.attention import attention
from alphafold3.model import features
from alphafold3.model import params
from alphafold3.model import post_processing
from alphafold3.model.components import base_model
from alphafold3.model.components import utils
from alphafold3.model.diffusion import model as diffusion_model
import haiku as hk
import jax
from jax import numpy as jnp
import numpy as np

import math
from concurrent import futures

_HOME_DIR = pathlib.Path(os.environ.get('HOME'))
DEFAULT_MODEL_DIR = _HOME_DIR / 'models'
DEFAULT_DB_DIR = _HOME_DIR / 'public_databases'

import logging

# Input and output paths.
_JSON_PATH = flags.DEFINE_string(
  'json_path',
  None,
  'Path to the input JSON file.',
)
_INPUT_DIR = flags.DEFINE_string(
  'input_dir',
  None,
  'Path to the directory containing input JSON files.',
)
_OUTPUT_DIR = flags.DEFINE_string(
  'output_dir',
  None,
  'Path to a directory where the results will be saved.',
)

_MODEL_DIR = flags.DEFINE_string(
  'model_dir',
  DEFAULT_MODEL_DIR.as_posix(),
  'Path to the model to use for inference.',
)

_FLASH_ATTENTION_IMPLEMENTATION = flags.DEFINE_enum(
  'flash_attention_implementation',
  default='triton',
  enum_values=['triton', 'cudnn', 'xla'],
  help=(
    "Flash attention implementation to use. 'triton' and 'cudnn' uses a"
    ' Triton and cuDNN flash attention implementation, respectively. The'
    ' Triton kernel is fastest and has been tested more thoroughly. The'
    " Triton and cuDNN kernels require Ampere GPUs or later. 'xla' uses an"
    ' XLA attention implementation (no flash attention) and is portable'
    ' across GPU devices.'
  ),
)

# Control which stages to run.
_RUN_DATA_PIPELINE = flags.DEFINE_bool(
    'run_data_pipeline',
    True,
    'Whether to run the data pipeline on the fold inputs.',
)
_RUN_INFERENCE = flags.DEFINE_bool(
    'run_inference',
    True,
    'Whether to run inference on the fold inputs.',
)


# Binary paths.
_HMMER_FOLDER_PATH = flags.DEFINE_string(
  'hmmer_folder_path',
  None,
  'Path to the hmmer folder binary.',
)

# Database paths.
_DB_DIR = flags.DEFINE_string(
  'db_dir',
  DEFAULT_DB_DIR.as_posix(),
  'Path to the directory containing the databases.',
)
_SMALL_BFD_DATABASE_PATH = flags.DEFINE_string(
  'small_bfd_database_path',
  '${DB_DIR}/bfd-first_non_consensus_sequences.fasta',
  'Small BFD database path, used for protein MSA search.',
)
_MGNIFY_DATABASE_PATH = flags.DEFINE_string(
  'mgnify_database_path',
  '${DB_DIR}/mgy_clusters_2022_05.fa',
  'Mgnify database path, used for protein MSA search.',
)
_UNIPROT_CLUSTER_ANNOT_DATABASE_PATH = flags.DEFINE_string(
  'uniprot_cluster_annot_database_path',
  '${DB_DIR}/uniprot_all_2021_04.fa',
  'UniProt database path, used for protein paired MSA search.',
)
_UNIREF90_DATABASE_PATH = flags.DEFINE_string(
  'uniref90_database_path',
  '${DB_DIR}/uniref90_2022_05.fa',
  'UniRef90 database path, used for MSA search. The MSA obtained by '
  'searching it is used to construct the profile for template search.',
)
_NTRNA_DATABASE_PATH = flags.DEFINE_string(
  'ntrna_database_path',
  '${DB_DIR}/nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta',
  'NT-RNA database path, used for RNA MSA search.',
)
_RFAM_DATABASE_PATH = flags.DEFINE_string(
  'rfam_database_path',
  '${DB_DIR}/rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta',
  'Rfam database path, used for RNA MSA search.',
)
_RNA_CENTRAL_DATABASE_PATH = flags.DEFINE_string(
  'rna_central_database_path',
  '${DB_DIR}/rnacentral_active_seq_id_90_cov_80_linclust.fasta',
  'RNAcentral database path, used for RNA MSA search.',
)
_PDB_DATABASE_PATH = flags.DEFINE_string(
  'pdb_database_path',
  '${DB_DIR}/pdb_2022_09_28_mmcif_files.tar',
  'PDB database directory with mmCIF files path, used for template search.',
)
_SEQRES_DATABASE_PATH = flags.DEFINE_string(
  'seqres_database_path',
  '${DB_DIR}/pdb_seqres_2022_09_28.fasta',
  'PDB sequence database path, used for template search.',
)

# Number of CPUs to use for MSA tools.
# _JACKHMMER_N_CPU = flags.DEFINE_integer(
#     'jackhmmer_n_cpu',
#     min(multiprocessing.cpu_count(), 8),
#     'Number of CPUs to use for Jackhmmer. Default to min(cpu_count, 8). Going'
#     ' beyond 8 CPUs provides very little additional speedup.',
# )
# _NHMMER_N_CPU = flags.DEFINE_integer(
#     'nhmmer_n_cpu',
#     min(multiprocessing.cpu_count(), 8),
#     'Number of CPUs to use for Nhmmer. Default to min(cpu_count, 8). Going'
#     ' beyond 8 CPUs provides very little additional speedup.',
# )
_TOTAL_N_CPU = flags.DEFINE_integer(
  'tot_n_cpu',
  min(multiprocessing.cpu_count(), 8),
  'Number of CPUs to use for all MSA tools. Default to min(cpu_count, 8)'
)
_MSA_N_CPU = flags.DEFINE_integer(
  'msa_n_cpu',
  min(multiprocessing.cpu_count(), 2),
  'Number of CPUs to use for each MSA tools. Default to min(cpu_count, 2)'
)

# Compilation cache
_JAX_COMPILATION_CACHE_DIR = flags.DEFINE_string(
  'jax_compilation_cache_dir',
  None,
  'Path to a directory for the JAX compilation cache.',
)

# Compilation buckets.
_BUCKETS = flags.DEFINE_list(
    'buckets',
    # pyformat: disable
    ['256', '512', '768', '1024', '1280', '1536', '2048', '2560', '3072',
     '3584', '4096', '4608', '5120'],
    # pyformat: enable
    'Strictly increasing order of token sizes for which to cache compilations.'
    ' For any input with more tokens than the largest bucket size, a new bucket'
    ' is created for exactly that number of tokens.',
)

class ConfigurableModel(Protocol):
  """A model with a nested config class."""

  class Config(base_config.BaseConfig):
    ...

  def __call__(self, config: Config) -> Self:
    ...

  @classmethod
  def get_inference_result(
      cls: Self,
      batch: features.BatchDict,
      result: base_model.ModelResult,
      target_name: str = '',
  ) -> Iterable[base_model.InferenceResult]:
    ...


ModelT = TypeVar('ModelT', bound=ConfigurableModel)


def make_model_config(
    *,
    model_class: type[ModelT] = diffusion_model.Diffuser,
    flash_attention_implementation: attention.Implementation = 'triton',
):
  config = model_class.Config()
  if hasattr(config, 'global_config'):
    config.global_config.flash_attention_implementation = (
        flash_attention_implementation
    )
  return config


class ModelRunner:
  """Helper class to run structure prediction stages."""

  def __init__(
      self,
      model_class: ConfigurableModel,
      config: base_config.BaseConfig,
      device: jax.Device,
      model_dir: pathlib.Path,
  ):
    self._model_class = model_class
    self._model_config = config
    self._device = device
    self._model_dir = model_dir

  @functools.cached_property
  def model_params(self) -> hk.Params:
    """Loads model parameters from the model directory."""
    return params.get_model_haiku_params(model_dir=self._model_dir)

  @functools.cached_property
  def _model(
      self,
  ) -> Callable[[jnp.ndarray, features.BatchDict], base_model.ModelResult]:
    """Loads model parameters and returns a jitted model forward pass."""
    assert isinstance(self._model_config, self._model_class.Config)

    @hk.transform
    def forward_fn(batch):
      result = self._model_class(self._model_config)(batch)
      result['__identifier__'] = self.model_params['__meta__']['__identifier__']
      return result

    return functools.partial(
        jax.jit(forward_fn.apply, device=self._device), self.model_params
    )

  def run_inference(
      self, featurised_example: features.BatchDict, rng_key: jnp.ndarray
  ) -> base_model.ModelResult:
    """Computes a forward pass of the model on a featurised example."""
    featurised_example = jax.device_put(
        jax.tree_util.tree_map(
            jnp.asarray, utils.remove_invalidly_typed_feats(featurised_example)
        ),
        self._device,
    )

    result = self._model(rng_key, featurised_example)
    result = jax.tree.map(np.asarray, result)
    result = jax.tree.map(
        lambda x: x.astype(jnp.float32) if x.dtype == jnp.bfloat16 else x,
        result,
    )
    result['__identifier__'] = result['__identifier__'].tobytes()
    return result

  def extract_structures(
      self,
      batch: features.BatchDict,
      result: base_model.ModelResult,
      target_name: str,
  ) -> list[base_model.InferenceResult]:
    """Generates structures from model outputs."""
    return list(
        self._model_class.get_inference_result(
            batch=batch, result=result, target_name=target_name
        )
    )


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ResultsForSeed:
  """Stores the inference results (diffusion samples) for a single seed.

  Attributes:
    seed: The seed used to generate the samples.
    inference_results: The inference results, one per sample.
    full_fold_input: The fold input that must also include the results of
      running the data pipeline - MSA and templates.
  """

  seed: int
  inference_results: Sequence[base_model.InferenceResult]
  full_fold_input: folding_input.Input


def predict_structure(
    fold_input: folding_input.Input,
    model_runner: ModelRunner,
    buckets: Sequence[int] | None = None,
) -> Sequence[ResultsForSeed]:
  """Runs the full inference pipeline to predict structures for each seed."""

  print(f'Featurising data for seeds {fold_input.rng_seeds}...')
  featurisation_start_time = time.time()
  ccd = chemical_components.cached_ccd(user_ccd=fold_input.user_ccd)
  featurised_examples = featurisation.featurise_input(
      fold_input=fold_input, buckets=buckets, ccd=ccd, verbose=True
  )
  print(
      f'Featurising data for seeds {fold_input.rng_seeds} took '
      f' {time.time() - featurisation_start_time:.2f} seconds.'
  )
  all_inference_start_time = time.time()
  all_inference_results = []
  for seed, example in zip(fold_input.rng_seeds, featurised_examples):
    print(f'Running model inference for seed {seed}...')
    inference_start_time = time.time()
    rng_key = jax.random.PRNGKey(seed)
    result = model_runner.run_inference(example, rng_key)
    print(
        f'Running model inference for seed {seed} took '
        f' {time.time() - inference_start_time:.2f} seconds.'
    )
    print(f'Extracting output structures (one per sample) for seed {seed}...')
    extract_structures = time.time()
    inference_results = model_runner.extract_structures(
        batch=example, result=result, target_name=fold_input.name
    )
    print(
        f'Extracting output structures (one per sample) for seed {seed} took '
        f' {time.time() - extract_structures:.2f} seconds.'
    )
    all_inference_results.append(
        ResultsForSeed(
            seed=seed,
            inference_results=inference_results,
            full_fold_input=fold_input,
        )
    )
    print(
        'Running model inference and extracting output structures for seed'
        f' {seed} took  {time.time() - inference_start_time:.2f} seconds.'
    )
  print(
      'Running model inference and extracting output structures for seeds'
      f' {fold_input.rng_seeds} took '
      f' {time.time() - all_inference_start_time:.2f} seconds.'
  )
  return all_inference_results


def write_fold_input_json(
    fold_input: folding_input.Input,
    output_dir: os.PathLike[str] | str,
) -> None:
  """Writes the input JSON to the output directory."""
  os.makedirs(output_dir, exist_ok=True)
  with open(
      os.path.join(output_dir, f'{fold_input.sanitised_name()}_data.json'), 'wt'
  ) as f:
    f.write(fold_input.to_json())


def write_outputs(
    all_inference_results: Sequence[ResultsForSeed],
    output_dir: os.PathLike[str] | str,
    job_name: str,
) -> None:
  """Writes outputs to the specified output directory."""
  ranking_scores = []
  max_ranking_score = None
  max_ranking_result = None

  output_terms = (
      pathlib.Path(alphafold3.cpp.__file__).parent / 'OUTPUT_TERMS_OF_USE.md'
  ).read_text()

  os.makedirs(output_dir, exist_ok=True)
  for results_for_seed in all_inference_results:
    seed = results_for_seed.seed
    for sample_idx, result in enumerate(results_for_seed.inference_results):
      sample_dir = os.path.join(output_dir, f'seed-{seed}_sample-{sample_idx}')
      os.makedirs(sample_dir, exist_ok=True)
      post_processing.write_output(
          inference_result=result, output_dir=sample_dir
      )
      ranking_score = float(result.metadata['ranking_score'])
      ranking_scores.append((seed, sample_idx, ranking_score))
      if max_ranking_score is None or ranking_score > max_ranking_score:
        max_ranking_score = ranking_score
        max_ranking_result = result

  if max_ranking_result is not None:  # True iff ranking_scores non-empty.
    post_processing.write_output(
        inference_result=max_ranking_result,
        output_dir=output_dir,
        # The output terms of use are the same for all seeds/samples.
        terms_of_use=output_terms,
        name=job_name,
    )
    # Save csv of ranking scores with seeds and sample indices, to allow easier
    # comparison of ranking scores across different runs.
    with open(os.path.join(output_dir, 'ranking_scores.csv'), 'wt') as f:
      writer = csv.writer(f)
      writer.writerow(['seed', 'sample', 'ranking_score'])
      writer.writerows(ranking_scores)


@overload
def process_fold_input(
    fold_input: folding_input.Input,
    data_pipeline_config: pipeline.DataPipelineConfig | None,
    model_runner: None,
    output_dir: os.PathLike[str] | str,
    buckets: Sequence[int] | None = None,
) -> folding_input.Input:
  ...


@overload
def process_fold_input(
    fold_input: folding_input.Input,
    data_pipeline_config: pipeline.DataPipelineConfig | None,
    model_runner: ModelRunner,
    output_dir: os.PathLike[str] | str,
    buckets: Sequence[int] | None = None,
) -> Sequence[ResultsForSeed]:
  ...


def process_fold_input(
    fold_input: folding_input.Input,
    data_pipeline_config: pipeline.DataPipelineConfig | None,
    model_runner: ModelRunner | None,
    output_dir: os.PathLike[str] | str,
    buckets: Sequence[int] | None = None,
) -> folding_input.Input | Sequence[ResultsForSeed]:
  """Runs data pipeline and/or inference on a single fold input.

  Args:
    fold_input: Fold input to process.
    data_pipeline_config: Data pipeline config to use. If None, skip the data
      pipeline.
    model_runner: Model runner to use. If None, skip inference.
    output_dir: Output directory to write to.
    buckets: Bucket sizes to pad the data to, to avoid excessive re-compilation
      of the model. If None, calculate the appropriate bucket size from the
      number of tokens. If not None, must be a sequence of at least one integer,
      in strictly increasing order. Will raise an error if the number of tokens
      is more than the largest bucket size.

  Returns:
    The processed fold input, or the inference results for each seed.

  Raises:
    ValueError: If the fold input has no chains.
  """
  print(f'Processing fold input {fold_input.name}')

  if not fold_input.chains:
    raise ValueError('Fold input has no chains.')

  if model_runner is not None:
    # If we're running inference, check we can load the model parameters before
    # (possibly) launching the data pipeline.
    print('Checking we can load the model parameters...')
    _ = model_runner.model_params

  if data_pipeline_config is None:
    print('Skipping data pipeline...')
  else:
    print('Running data pipeline...')
    fold_input = pipeline.DataPipeline(data_pipeline_config).process(fold_input, cpu_pool_num, sub_pool_condition)

  print(f'Output directory: {output_dir}')
  print(f'Writing model input JSON to {output_dir}')
  write_fold_input_json(fold_input, output_dir)
  if model_runner is None:
    print('Skipping inference...')
    output = fold_input
  else:
    print(
        f'Predicting 3D structure for {fold_input.name} for seed(s)'
        f' {fold_input.rng_seeds}...'
    )
    all_inference_results = predict_structure(
        fold_input=fold_input,
        model_runner=model_runner,
        buckets=buckets,
    )
    print(
        f'Writing outputs for {fold_input.name} for seed(s)'
        f' {fold_input.rng_seeds}...'
    )
    write_outputs(
        all_inference_results=all_inference_results,
        output_dir=output_dir,
        job_name=fold_input.sanitised_name(),
    )
    output = all_inference_results

  print(f'Done processing fold input {fold_input.name}.')
  return output


def main(_):
  _JACKHMMER_BINARY_PATH = flags.DEFINE_string(
    'jackhmmer_binary_path',
    shutil.which('jackhmmer', path=_HMMER_FOLDER_PATH.value),
    'Path to the Jackhmmer binary.',
  )
  _NHMMER_BINARY_PATH = flags.DEFINE_string(
    'nhmmer_binary_path',
    shutil.which('nhmmer', path=_HMMER_FOLDER_PATH.value),
    'Path to the Nhmmer binary.',
  )
  _HMMALIGN_BINARY_PATH = flags.DEFINE_string(
    'hmmalign_binary_path',
    shutil.which('hmmalign', path=_HMMER_FOLDER_PATH.value),
    'Path to the Hmmalign binary.',
  )
  _HMMSEARCH_BINARY_PATH = flags.DEFINE_string(
    'hmmsearch_binary_path',
    shutil.which('hmmsearch', path=_HMMER_FOLDER_PATH.value),
    'Path to the Hmmsearch binary.',
  )
  _HMMBUILD_BINARY_PATH = flags.DEFINE_string(
    'hmmbuild_binary_path',
    shutil.which('hmmbuild', path=_HMMER_FOLDER_PATH.value),
    'Path to the Hmmbuild binary.',
  )

  if _JSON_PATH.value is None == _INPUT_DIR.value is None:
    raise ValueError(
        'Exactly one of --json_path or --input_dir must be specified.'
    )

  if not _RUN_INFERENCE.value and not _RUN_DATA_PIPELINE.value:
    raise ValueError(
        'At least one of --run_inference or --run_data_pipeline must be'
        ' set to true.'
    )

  if _INPUT_DIR.value is not None:
    fold_inputs = folding_input.load_fold_inputs_from_dir(
        pathlib.Path(_INPUT_DIR.value)
    )
  elif _JSON_PATH.value is not None:
    fold_inputs = folding_input.load_fold_inputs_from_path(
        pathlib.Path(_JSON_PATH.value)
    )
  else:
    raise AssertionError(
        'Exactly one of --json_path or --input_dir must be specified.'
    )

  # Make sure we can create the output directory before running anything.
  try:
    os.makedirs(_OUTPUT_DIR.value, exist_ok=True)
  except OSError as e:
    print(f'Failed to create output directory {_OUTPUT_DIR.value}: {e}')
    raise

  notice = textwrap.wrap(
      'Running AlphaFold 3. Please note that standard AlphaFold 3 model'
      ' parameters are only available under terms of use provided at'
      ' https://github.com/google-deepmind/alphafold3/blob/main/WEIGHTS_TERMS_OF_USE.md.'
      ' If you do not agree to these terms and are using AlphaFold 3 derived'
      ' model parameters, cancel execution of AlphaFold 3 inference with'
      ' CTRL-C, and do not use the model parameters.',
      break_long_words=False,
      break_on_hyphens=False,
      width=80,
  )
  print('\n'.join(notice))

  replace_db_dir = lambda x: string.Template(x).substitute(
      DB_DIR=_DB_DIR.value
  )
  data_pipeline_config = pipeline.DataPipelineConfig(
      jackhmmer_binary_path=_JACKHMMER_BINARY_PATH.value,
      nhmmer_binary_path=_NHMMER_BINARY_PATH.value,
      hmmalign_binary_path=_HMMALIGN_BINARY_PATH.value,
      hmmsearch_binary_path=_HMMSEARCH_BINARY_PATH.value,
      hmmbuild_binary_path=_HMMBUILD_BINARY_PATH.value,
      small_bfd_database_path=replace_db_dir(_SMALL_BFD_DATABASE_PATH.value),
      mgnify_database_path=replace_db_dir(_MGNIFY_DATABASE_PATH.value),
      uniprot_cluster_annot_database_path=replace_db_dir(
          _UNIPROT_CLUSTER_ANNOT_DATABASE_PATH.value
      ),
      uniref90_database_path=replace_db_dir(_UNIREF90_DATABASE_PATH.value),
      ntrna_database_path=replace_db_dir(_NTRNA_DATABASE_PATH.value),
      rfam_database_path=replace_db_dir(_RFAM_DATABASE_PATH.value),
      rna_central_database_path=replace_db_dir(
          _RNA_CENTRAL_DATABASE_PATH.value
      ),
      pdb_database_path=replace_db_dir(_PDB_DATABASE_PATH.value),
      seqres_database_path=replace_db_dir(_SEQRES_DATABASE_PATH.value),
      jackhmmer_n_cpu=_MSA_N_CPU.value,
      nhmmer_n_cpu=_MSA_N_CPU.value,
  )

  devices = jax.local_devices(backend='gpu')
  print(f'Found local devices: {devices}')

  print('Building model from scratch...')
  model_queue = multiprocessing.Queue()
  for i in range(len(devices)):
    model_queue.put(i)

  print(f'Processing {len(fold_inputs)} fold inputs.')

  _buckets_tuple = tuple(int(bucket) for bucket in _BUCKETS.value)
  if _RUN_INFERENCE.value is not None and _RUN_DATA_PIPELINE is not None:
    _cpu_pool_num = multiprocessing.Value('i', max(1, math.floor(_TOTAL_N_CPU.value / _MSA_N_CPU.value)))
    _sub_pool_condition = multiprocessing.Condition()
    with futures.ProcessPoolExecutor(max_workers=len(devices),
                                     initializer=gpu_model_init, initargs=(model_queue,
                                                                           _FLASH_ATTENTION_IMPLEMENTATION.value,
                                                                           pathlib.Path(_MODEL_DIR.value),
                                                                           _JAX_COMPILATION_CACHE_DIR.value,
                                                                           _buckets_tuple)) as gpu_executor:
      with futures.ProcessPoolExecutor(max_workers=_TOTAL_N_CPU.value,
                                       initializer=cpu_model_init, initargs=(_cpu_pool_num, _sub_pool_condition, _buckets_tuple)) as cpu_executor:
        task_list = [
          cpu_executor.submit(cpu_preprocess, fold_input, data_pipeline_config,
                              os.path.join(_OUTPUT_DIR.value, fold_input.sanitised_name()))
        for fold_input in fold_inputs]

        gpu_task_list = []
        for task in futures.as_completed(task_list):
          pro_fold_input = task.result()
          logging.info(f'CPU task queue out {pro_fold_input.sanitised_name()}')
          gpu_task = gpu_executor.submit(gpu_process,
                                         pro_fold_input, _OUTPUT_DIR.value)
          logging.info(f'GPU task put {pro_fold_input.sanitised_name()}')
          gpu_task_list.append(gpu_task)

        for task in gpu_task_list:
          task.result()

  elif _RUN_INFERENCE.value is None:
    with futures.ProcessPoolExecutor(max_workers=len(devices),
                                     initializer=gpu_model_init, initargs=(model_queue,
                                                                           _FLASH_ATTENTION_IMPLEMENTATION.value,
                                                                           pathlib.Path(_MODEL_DIR.value),
                                                                           _JAX_COMPILATION_CACHE_DIR.value,
                                                                           _buckets_tuple)) as gpu_executor:
      gpu_task_list = []
      for fold_input in fold_inputs:
        gpu_task = gpu_executor.submit(gpu_process,
                                       fold_input, _OUTPUT_DIR.value)
        gpu_task_list.append(gpu_task)

      for task in gpu_task_list:
        task.result()

  elif _RUN_DATA_PIPELINE.value is None:
    _cpu_pool_num = multiprocessing.Value('i', max(1, math.floor(_TOTAL_N_CPU.value / _MSA_N_CPU.value)))
    _sub_pool_condition = multiprocessing.Condition()
    with futures.ProcessPoolExecutor(max_workers=_TOTAL_N_CPU.value,
                                     initializer=cpu_model_init,
                                     initargs=(_cpu_pool_num, _sub_pool_condition, _buckets_tuple)) as cpu_executor:
      task_list = [
        cpu_executor.submit(cpu_preprocess, fold_input, data_pipeline_config, _OUTPUT_DIR.valu)
        for fold_input in fold_inputs]

      for task in task_list:
        task.result()

  print(f'Done processing {len(fold_inputs)} fold inputs.')

def cpu_model_init(_cpu_pool_num, _sub_pool_condition, _buckets):
  global cpu_pool_num, sub_pool_condition, buckets
  cpu_pool_num = _cpu_pool_num
  sub_pool_condition = _sub_pool_condition
  buckets = _buckets

def gpu_model_init(model_queue, _flash_attention_implementation_value, _real_model_dir, _jax_compilation_cache_dir_value, _buckets):
  os.environ["CUDA_VISIBLE_DEVICES"] = str(model_queue.get())
  devices = jax.local_devices(backend='gpu')

  assert len(devices) == 1
  print(f'GPU init {multiprocessing.current_process().pid}', flush=True)
  global model_runner, buckets
  model_runner = \
    ModelRunner(
      model_class=diffusion_model.Diffuser,
      config=make_model_config(
        flash_attention_implementation=typing.cast(
          attention.Implementation, _flash_attention_implementation_value
        )
      ),
      device=devices[0],
      model_dir=_real_model_dir
    )

  buckets = _buckets
  if _jax_compilation_cache_dir_value is not None:
    jax.config.update(
        'jax_compilation_cache_dir', _jax_compilation_cache_dir_value
    )

def cpu_preprocess(fold_input, data_pipeline_config, _output_dir):
  logging.info(f'CPU task start {fold_input.sanitised_name()}')
  pro_fold_input = process_fold_input(
    fold_input=fold_input,
    data_pipeline_config=data_pipeline_config,
    model_runner=None,
    output_dir=_output_dir,
    buckets=buckets,
  )
  logging.info(f'CPU task end {fold_input.sanitised_name()}')
  return pro_fold_input

def gpu_process(fold_input, _output_dir):
  print(f'GPU start {multiprocessing.current_process().pid}', flush=True)
  process_fold_input(
    fold_input=fold_input,
    data_pipeline_config=None,
    model_runner=model_runner,
    output_dir=os.path.join(_output_dir, fold_input.sanitised_name()),
    buckets=buckets,
  )
  print(f'GPU end {multiprocessing.current_process().pid}', flush=True)

if __name__ == '__main__':
  multiprocessing.set_start_method('spawn')
  flags.mark_flags_as_required([
    'output_dir',
  ])
  app.run(main)