"""Define word-based embedders."""

import abc
import collections
import os

import numpy as np
import tensorflow as tf

from google.protobuf import text_format
from tensorboard.plugins import projector

from yimt.core import tokenizers
from yimt.core import constants
from yimt.core.data import dataset as dataset_util
from yimt.core.data import text
from yimt.core.data import vocab
from yimt.core.inputters import Inputter
from yimt.core.layers import common
from yimt.core.utils import misc


def save_embeddings_metadata(
    log_dir, variable_name, vocabulary_file, num_oov_buckets=1
):
    """Registers an embedding variable for visualization in TensorBoard.

    This function registers :obj:`variable_name` in the ``projector_config.pbtxt``
    file and generates metadata from :obj:`vocabulary_file` to attach a label
    to each word ID.

    Args:
      log_dir: The active log directory.
      variable_name: The variable name in the checkpoint.
      vocabulary_file: The associated vocabulary file.
      num_oov_buckets: The number of additional unknown tokens.
    """
    # Assume it ends with /.ATTRIBUTES/VALUE
    filename = "%s.txt" % "_".join(variable_name.split("/")[:-2])
    metadata_path = os.path.join(log_dir, filename)

    with tf.io.gfile.GFile(vocabulary_file) as src, tf.io.gfile.GFile(
        metadata_path, mode="w"
    ) as dst:
        ws_index = 0
        for line in src:
            # The TensorBoard code checks line.trim().length == 0 when loading the
            # metadata file so make sure lines are not dropped.
            if not line.replace("\uFEFF", "").strip():
                dst.write("<whitespace%d>\n" % ws_index)
                ws_index += 1
            else:
                dst.write(line)
        if num_oov_buckets == 1:
            dst.write("<unk>\n")
        else:
            for i in range(num_oov_buckets):
                dst.write("<unk%d>\n" % i)

    config = projector.ProjectorConfig()

    # If the projector file exists, load it.
    config_path = os.path.join(log_dir, "projector_config.pbtxt")
    if tf.io.gfile.exists(config_path):
        with tf.io.gfile.GFile(config_path) as config_file:
            text_format.Merge(config_file.read(), config)

    # If this embedding is already registered, just update the metadata path.
    exists = False
    for meta in config.embeddings:
        if meta.tensor_name == variable_name:
            meta.metadata_path = filename
            exists = True
            break

    if not exists:
        embedding = config.embeddings.add()
        embedding.tensor_name = variable_name
        embedding.metadata_path = filename

    with tf.io.gfile.GFile(config_path, "w") as config_file:
        config_file.write(text_format.MessageToString(config))


def load_pretrained_embeddings(
    embedding_file,
    vocabulary_file,
    num_oov_buckets=1,
    with_header=True,
    case_insensitive_embeddings=True,
):
    """Returns pretrained embeddings relative to the vocabulary.

    The :obj:`embedding_file` must have the following format:

    .. code-block:: text

        N M
        word1 val1 val2 ... valM
        word2 val1 val2 ... valM
        ...
        wordN val1 val2 ... valM

    or if :obj:`with_header` is ``False``:

    .. code-block:: text

        word1 val1 val2 ... valM
        word2 val1 val2 ... valM
        ...
        wordN val1 val2 ... valM

    This function will iterate on each embedding in :obj:`embedding_file` and
    assign the pretrained vector to the associated word in :obj:`vocabulary_file`
    if found. Otherwise, the embedding is ignored.

    If :obj:`case_insensitive_embeddings` is ``True``, word embeddings are assumed
    to be trained on lowercase data. In that case, word alignments are case
    insensitive meaning the pretrained word embedding for "the" will be assigned
    to "the", "The", "THE", or any other case variants included in
    :obj:`vocabulary_file`.

    Args:
      embedding_file: Path the embedding file. Entries will be matched against
        :obj:`vocabulary_file`.
      vocabulary_file: The vocabulary file containing one word per line.
      num_oov_buckets: The number of additional unknown tokens.
      with_header: ``True`` if the embedding file starts with a header line like
        in GloVe embedding files.
      case_insensitive_embeddings: ``True`` if embeddings are trained on lowercase
        data.

    Returns:
      A Numpy array of shape ``[vocabulary_size + num_oov_buckets, embedding_size]``.
    """
    # Map words to ids from the vocabulary.
    word_to_id = collections.defaultdict(list)
    with tf.io.gfile.GFile(vocabulary_file) as vocabulary:
        count = 0
        for word in vocabulary:
            word = word.strip()
            if case_insensitive_embeddings:
                word = word.lower()
            word_to_id[word].append(count)
            count += 1

    # Fill pretrained embedding matrix.
    with tf.io.gfile.GFile(embedding_file) as embedding:
        pretrained = None

        if with_header:
            next(embedding)

        for line in embedding:
            fields = line.strip().split()
            word = fields[0]

            if pretrained is None:
                pretrained = np.random.normal(
                    size=(count + num_oov_buckets, len(fields) - 1)
                )

            # Lookup word in the vocabulary.
            if word in word_to_id:
                ids = word_to_id[word]
                for index in ids:
                    pretrained[index] = np.asarray(fields[1:])

    return pretrained


def add_sequence_controls(
    ids,
    length=None,
    start_id=None,
    end_id=None,
    pad_id=constants.PADDING_ID,
):
    """Adds sequence control tokens.

    Args:
      ids: Sequence of token ids as a 1D ``tf.Tensor``, a 2D ``tf.Tensor``, or a
        2D ``tf.RaggedTensor``.
      length: Length of each sequence in the batch as a 1D ``tf.Tensor``.
        Required if :obj:`ids` is a 2D ``tf.Tensor``.
      start_id: Token id to prepend to the sequences (set ``None`` to disable).
      end_id: Token id to append to the sequences (set ``None`` to disable).
      pad_id: Padding token ID.

    Returns:
      The updated token ids, and optionally the updated length if it was initially
      passed as argument.

    Raises:
      ValueError: if :obj:`ids` is a dense 2D ``tf.Tensor``, but :obj:`length` is not set.
      ValueError: if the rank of :obj:`ids` is not 1 or 2.
    """
    input_is_ragged = isinstance(ids, tf.RaggedTensor)
    if start_id is None and end_id is None:
        return ids if input_is_ragged or length is None else (ids, length)

    batch_size = None
    rank = ids.shape.rank
    if rank == 2:
        if not input_is_ragged:
            if length is None:
                raise ValueError(
                    "length argument is required when passing a dense batch of sequences"
                )
            ids = tf.RaggedTensor.from_tensor(ids, lengths=length)
        batch_size = ids.nrows()
    elif rank != 1:
        raise ValueError("Unsupported rank %d (expected 1 or 2)" % rank)

    def _make_column(value):
        value = tf.constant(value, dtype=ids.dtype)
        if batch_size is not None:
            value = tf.fill([batch_size], value)
        return tf.expand_dims(value, -1)

    concat_inputs = []
    if start_id is not None:
        concat_inputs.append(_make_column(start_id))
    concat_inputs.append(ids)
    if end_id is not None:
        concat_inputs.append(_make_column(end_id))

    ids = tf.concat(concat_inputs, axis=-1)

    if not input_is_ragged:
        if batch_size is not None:
            ids = ids.to_tensor(default_value=pad_id)
        if length is not None:
            length += len(concat_inputs) - 1
            return ids, length

    return ids


class TextInputter(Inputter):
    """An abstract inputter that processes text."""

    def __init__(self, num_oov_buckets=1, **kwargs):
        super().__init__(**kwargs)
        self.num_oov_buckets = num_oov_buckets
        self.noiser = None
        self.in_place_noise = False
        self.noise_probability = None
        self.vocabulary_file = None
        self.vocabulary_size = None
        self.tokens_to_ids = None
        self.ids_to_tokens = None
        self.tokenizer = None

    def initialize(self, data_config):
        self.vocabulary_file = data_config["vocabulary"]
        (
            self.vocabulary_size,
            self.tokens_to_ids,
            self.ids_to_tokens,
        ) = vocab.create_lookup_tables(
            self.vocabulary_file,
            self.num_oov_buckets,
            as_asset=data_config.get("export_vocabulary_assets", True),
        )
        tokenizer_config = data_config.get("tokenization")
        self.tokenizer = tokenizers.make_tokenizer(tokenizer_config)

    def set_noise(self, noiser, in_place=True, probability=None):
        """Enables noise to be applied to the input features.

        Args:
          noiser: A :class:`yimt.data.WordNoiser` instance.
          in_place: If ``False``, the noisy version of the input will be stored as
            a separate feature prefixed with ``noisy_``.
          probability: When :obj:`in_place` is enabled, the probability to apply the
            noise.

        Raises:
          ValueError: if :obj:`in_place` is enabled but a :obj:`probability` is not
            set.
        """
        if in_place and probability is None:
            raise ValueError("In-place noise requires a probability")
        self.noiser = noiser
        self.in_place_noise = in_place
        self.noise_probability = probability

    def export_assets(self, asset_dir):
        self._assert_is_initialized()
        return self.tokenizer.export_assets(asset_dir, asset_prefix=self.asset_prefix)

    def make_dataset(self, data_file, training=None):
        return dataset_util.make_datasets(tf.data.TextLineDataset, data_file)

    def get_dataset_size(self, data_file):
        if isinstance(data_file, list):
            return list(map(misc.count_lines, data_file))
        return misc.count_lines(data_file)

    def has_prepare_step(self):
        # For performance reasons, we apply external tokenizers on a batch of
        # dataset elements during the preparation step.
        return not self.tokenizer.in_graph and not isinstance(
            self.tokenizer, tokenizers.SpaceTokenizer
        )

    def prepare_elements(self, elements, training=None):
        return {"tokens": self.tokenizer.tokenize(elements, training=training)}

    def make_features(self, element=None, features=None, training=None):
        """Tokenizes raw text."""
        self._assert_is_initialized()
        if features is None:
            features = {}
        if "tokens" in features:
            return features

        element = features.pop("text", element)
        if isinstance(element, dict):
            tokens = element["tokens"]
        else:
            element = tf.convert_to_tensor(element, dtype=tf.string)
            tokens = self.tokenizer.tokenize(element, training=training)

        if isinstance(tokens, tf.RaggedTensor):
            length = tokens.row_lengths()
            tokens = tokens.to_tensor(default_value=constants.PADDING_TOKEN)
        else:
            length = tf.shape(tokens)[0]
        if training and self.noiser is not None:
            noisy_tokens, noisy_length = self.noiser(
                tokens,
                sequence_length=length,
                probability=self.noise_probability if self.in_place_noise else None,
            )
            if self.in_place_noise:
                tokens = noisy_tokens
                length = noisy_length
            else:
                # Call make_features again to fill the remaining noisy features.
                noisy_features = dict(tokens=noisy_tokens, length=noisy_length)
                noisy_features = self.make_features(
                    features=noisy_features, training=training
                )
                for key, value in noisy_features.items():
                    features["noisy_%s" % key] = value
        features["length"] = length
        features["tokens"] = tokens
        return features

    def input_signature(self):
        self._assert_is_initialized()
        if self.tokenizer.in_graph:
            return {"text": tf.TensorSpec([None], tf.string)}
        else:
            return {
                "tokens": tf.TensorSpec([None, None], tf.string),
                "length": tf.TensorSpec([None], tf.int32),
            }

    def _assert_is_initialized(self):
        if self.tokenizer is None:
            raise RuntimeError(
                "The input layer is not initialized. You should initialize "
                "the model by calling model.initialize(data_config)."
            )


def _format_bool(b):
    return "yes" if b else "no"


class WordEmbedder(TextInputter):
    """Simple word embedder."""

    def __init__(self, embedding_size=None, dropout=0.0, **kwargs):
        """Initializes the parameters of the word embedder.

        Args:
          embedding_size: The size of the resulting embedding.
            If ``None``, an embedding file must be provided.
          dropout: The probability to drop units in the embedding.
          **kwargs: Additional layer keyword arguments.
        """
        super().__init__(**kwargs)
        self.embedding_size = embedding_size
        self.embedding_file = None
        self.dropout = dropout
        self.decoder_mode = False
        self.mark_start = None
        self.mark_end = None

    def set_decoder_mode(self, enable=True, mark_start=None, mark_end=None):
        """Make this inputter produce sequences for a decoder.

        In this mode, the returned "ids_out" feature is the decoder output sequence
        and "ids" is the decoder input sequence.

        Args:
          enable: Enable the decoder mode.
          mark_start: Mark the sequence start. If ``None``, keep the current value.
          mark_end: Mark the sequence end. If ``None``, keep the current value.
        """
        self.decoder_mode = enable
        if mark_start is not None:
            self.mark_start = mark_start
        if mark_end is not None:
            self.mark_end = mark_end

    def get_length(self, features, ignore_special_tokens=False):
        length = features["length"]
        if ignore_special_tokens:
            # Decoder mode shifts the sequences by one timesteps.
            num_special_tokens = -1 if self.decoder_mode else 0
            if self.mark_start:
                num_special_tokens += 1
            if self.mark_end:
                num_special_tokens += 1
            length -= num_special_tokens
        return length

    def get_oov_tokens(self, features):
        tokens, ids = features["tokens"], features["ids"]
        if self.mark_start:
            ids = ids[1:] if ids.shape.rank == 1 else ids[:, 1:]
        oov_tokens_pos = tf.where(ids >= (self.vocabulary_size - self.num_oov_buckets))
        return tf.gather_nd(tokens, oov_tokens_pos)

    def initialize(self, data_config):
        super().initialize(data_config)
        embedding = data_config.get("embedding")
        if embedding is None and self.embedding_size is None:
            raise ValueError("embedding_size must be set")
        if embedding is not None:
            self.embedding_file = embedding["path"]
            self.trainable = embedding.get("trainable", True)
            self.embedding_file_with_header = embedding.get("with_header", True)
            self.case_insensitive_embeddings = embedding.get("case_insensitive", True)
        sequence_controls = data_config.get("sequence_controls")
        if sequence_controls:
            self.mark_start = sequence_controls.get("start")
            self.mark_end = sequence_controls.get("end")

        tf.get_logger().info(
            "Initialized %s input layer:", self.asset_prefix.strip("_")
        )
        tf.get_logger().info(" - vocabulary size: %d", self.vocabulary_size)
        tf.get_logger().info(
            " - special tokens: BOS=%s, EOS=%s",
            _format_bool(self.mark_start),
            _format_bool(self.mark_end),
        )

    def make_features(self, element=None, features=None, training=None):
        """Converts words tokens to ids."""
        features = super().make_features(
            element=element, features=features, training=training
        )
        if "ids" not in features:
            features["ids"] = self.tokens_to_ids.lookup(features["tokens"])
            if self.mark_start or self.mark_end:
                features["ids"], features["length"] = add_sequence_controls(
                    features["ids"],
                    features["length"],
                    start_id=constants.START_OF_SENTENCE_ID
                    if self.mark_start
                    else None,
                    end_id=constants.END_OF_SENTENCE_ID if self.mark_end else None,
                )
        if self.decoder_mode:
            ids = features["ids"]
            length = features["length"]
            if ids.shape.rank == 2:
                mask = tf.sequence_mask(
                    length - 1, maxlen=tf.shape(ids)[1], dtype=ids.dtype
                )
                features["ids"] = (ids * mask)[:, :-1]
                features["ids_out"] = ids[:, 1:]
            else:
                features["ids"] = ids[:-1]
                features["ids_out"] = ids[1:]
            features["length"] -= 1
        return features

    def build(self, input_shape):
        if self.embedding_file:
            pretrained = load_pretrained_embeddings(
                self.embedding_file,
                self.vocabulary_file,
                num_oov_buckets=self.num_oov_buckets,
                with_header=self.embedding_file_with_header,
                case_insensitive_embeddings=self.case_insensitive_embeddings,
            )
            self.embedding_size = pretrained.shape[-1]
            initializer = tf.constant_initializer(value=pretrained.astype(self.dtype))
        else:
            initializer = None
        self.embedding = self.add_weight(
            "embedding",
            [self.vocabulary_size, self.embedding_size],
            initializer=initializer,
            trainable=self.trainable,
        )
        super().build(input_shape)

    def call(self, features, training=None):
        ids = features["ids"]

        if getattr(self, "_tflite_mode", False):
            # Workaround for TensorFlow issue #42410.
            ids = tf.expand_dims(ids, axis=-1)
            return tf.gather_nd(self.embedding, ids)

        outputs = tf.nn.embedding_lookup(self.embedding, ids)
        outputs = common.dropout(outputs, self.dropout, training=training)
        return outputs

    def visualize(self, model_root, log_dir):
        save_embeddings_metadata(
            log_dir,
            misc.get_variable_name(self.embedding, model_root),
            self.vocabulary_file,
            num_oov_buckets=self.num_oov_buckets,
        )

    def map_v1_weights(self, weights):
        return [(self.embedding, weights["w_embs"])]
