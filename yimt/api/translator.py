import os
import threading

import ctranslate2
import tensorflow as tf
import sentencepiece as spm
from yimt.core import load_config
from yimt.core.config import MODEL_DESCRIPTION_FILENAME, load_model_from_file
from yimt.core.utils import checkpoint as checkpoint_util
from word2word import Word2word

from yimt.api.text_splitter import paragraph_detokenizer, paragraph_tokenizer


class WordTranslator(object):
    """Word/Phrase Translator"""

    def __init__(self, source_lang, target_lang):
        self.lexicon = Word2word(source_lang, target_lang)

    def has(self, w):
        """Does the word exist?"""
        w = w.lower()
        return w in self.lexicon.word2x

    def lookup(self, w):
        """Get the translation of the word"""
        w = w.lower()
        return self.lexicon(w)[0]


def load_translator(model_or_config_dir, sp_src_path, lang_pair=None):
    """Create translator form config or model

    Args:
        param model_or_config_dir: model directory or config yaml file
        sp_src_path: SentencePiece model file for source language
        lang_pair: lang pair supported by translator

    Returns:
        a Translator
    """
    if ctranslate2.contains_model(model_or_config_dir):  # CTranslate2 model
        return TranslatorCT2(model_or_config_dir, sp_src_path, lang_pair)
    elif os.path.exists(os.path.join(model_or_config_dir, "saved_model.pb")):  # SavedModel
        return TranslatorSaved(model_or_config_dir, sp_src_path, lang_pair)
    else:  # checkpoint
        return TranslatorCkpt(model_or_config_dir, sp_src_path, lang_pair)


# Access translation function mutually
mutex = threading.Lock()


class Translator(object):
    """Translator base class"""

    def __init__(self, sp_src_path, lang_pair=None, batch_size=64):
        """
        Args:
            sp_src_path: SentencePiece model file for source language
            lang_pair: lang pair supported by this translator
            batch_size: the size of translation batch
        """
        self.sp_source_model = spm.SentencePieceProcessor(model_file=sp_src_path)
        self.lang_pair = lang_pair
        self.from_lang = "en"
        self.to_lang = "zh"
        if self.lang_pair is not None:
            self.from_lang = self.lang_pair.split("-")[0]
            self.to_lang = self.lang_pair.split("-")[1]
        self.batch_size = batch_size

    def _translate_batch(self, texts):
        """Translates a batch of texts.

        Args:
             texts: list of text or sentence
        Returns:
             list of translation text
        """
        raise NotImplementedError()

    def translate_list(self, texts):
        """Translate a list of text batch by batch

        maybe call translate_batch several times

        Args:
             texts: list of text
        """
        total = len(texts)
        done = 0
        results = []
        for i in range(0, len(texts), self.batch_size):
            if i+self.batch_size<len(texts):
                to_translate = texts[i:i+self.batch_size]
            else:
                to_translate = texts[i:]

            translations = self._translate_batch(to_translate)

            translations = self._post_zh(translations)

            results.extend(translations)
            done += len(to_translate)
        return results

    def _post_zh(self, translations):
        if self.to_lang == "zh":
            new_translations = []
            for t in translations:
                t = t.replace(",", "，")
                t = t.replace(";", "；")
                t = t.replace(":", "：")
                new_translations.append(t)
            return new_translations
        else:
            return translations

    def translate_paragraph(self, src):
        """Translate text paragraphs

        the text will be segmented into paragraphs, and then paragraph segmented into sentences.
        the format of text will be remained.

        Args:
            src: text to be translated

        Returns:
             translated text with paragraphs
        """
        source_sents, breaks = paragraph_tokenizer(src, self.from_lang)

        with mutex:
            translations = self.translate_list(source_sents)

        translation = paragraph_detokenizer(translations, breaks)

        return translation

    def _tokenize(self, text):
        """Tokenize string

        Args:
            text: string or list of string

        Returns:
             list of token list
        """
        if not isinstance(text, (list, tuple)):
            text = [text]
        tokens = self.sp_source_model.encode(text, out_type=str)
        return tokens

    def _preprocess(self, texts):
        """Prepare input for model

        :param texts list of text
        :return dict with tokens and length
        """
        all_tokens = self._tokenize(texts)
        lengths = [len(ts) for ts in all_tokens]
        max_length = max(lengths)

        for tokens, length in zip(all_tokens, lengths):  # pad
            if length < max_length:
                tokens += [""] * (max_length - length)

        inputs = {
            "tokens": tf.constant(all_tokens, dtype=tf.string),
            "length": tf.constant(lengths, dtype=tf.int32),
        }

        return inputs

    def _detokenize(self, tokens):
        """Detokenize tokens into string

        Args:
            tokens: list of token

        Returns:
             string
        """
        return " ".join(tokens).replace(" ", "").replace("▁", " ").strip()

    def _postprocess(self, outputs):
        """Process output of model

        :param outputs: output of model, a dict with tokens and length
        :return: list of translation text
        """
        texts = []
        for tokens, length in zip(outputs["tokens"].numpy(), outputs["length"].numpy()):
            tokens = tokens[0][: length[0]].tolist()
            tokens = [str(t, "utf-8") for t in tokens]
            texts.append(self._detokenize(tokens))
        return texts


def get_model_from_checkpoint(config_file):
    config = load_config([config_file])

    model_dir = config["model_dir"]
    model_description_path = os.path.join(model_dir, MODEL_DESCRIPTION_FILENAME)

    model = load_model_from_file(model_description_path, as_builder=False)

    model.initialize(config["data"], params={})

    checkpoint = checkpoint_util.Checkpoint.from_config(config, model)
    checkpoint_path = model_dir
    checkpoint.restore(checkpoint_path=checkpoint_path, weights_only=True)

    return model


class TranslatorCkpt(Translator):
    """A checkpoint based translator with SentencePieced tokenization"""

    def __init__(self, config_file, sp_src_path, lang_pair):
        """Load model

        :param config_file: configuration file path
        :param sp_src_path: SentencePiece model file for source language
        """
        super().__init__(sp_src_path, lang_pair)
        self._model = get_model_from_checkpoint(config_file)

    def _translate_batch(self, texts):
        # tokenize
        inputs = self._preprocess(texts)

        # encode
        features = self._model.features_inputter.make_features(features=inputs.copy())

        # translate
        _, outputs = self._model(features)

        # detokenize
        texts = self._postprocess(outputs)
        return texts


class TranslatorSaved(Translator):
    """A TF SavedModel based translator with SentencePieced tokenization"""

    def __init__(self, export_dir, sp_src_path, lang_pair):
        super().__init__(sp_src_path, lang_pair)

        self._imported = tf.saved_model.load(export_dir)
        self._translate_fn = self._imported.signatures["serving_default"]

    def _translate_batch(self, texts):
        """Translates a batch of texts."""
        inputs = self._preprocess(texts)
        outputs = self._translate_fn(**inputs)
        return self._postprocess(outputs)


class TranslatorCT2(Translator):
    """A CTranslate2 based translator with SentencePieced tokenization"""

    def __init__(self, model_dir, sp_src_path, lang_pair):
        """load model

        :param model_dir: ctranslate2 model directory
        :param sp_src_path: SentencePiece model file for source language
        """
        super().__init__(sp_src_path, lang_pair)

        if ctranslate2.contains_model(model_dir):
            # self.cpu_num = os.cpu_count()
            # self.inter_threads = 2
            # self.intra_threads = int(self.cpu_num / 2)

            self.translator = ctranslate2.Translator(
                model_dir,
                device="cuda",
                # inter_threads=self.inter_threads,
                # intra_threads=self.intra_threads,
            )

    def _translate_batch(self, texts):
        source_sents_tok = self._tokenize(texts)

        translations_tok = self.translator.translate_batch(
            source=source_sents_tok,
            beam_size=5,
            batch_type="tokens",
            max_batch_size=1024,
            replace_unknowns=True,
            repetition_penalty=1.2,
            target_prefix=None,
        )

        translations = [self._detokenize(translation[0]["tokens"])
                        for translation in translations_tok]

        return translations
