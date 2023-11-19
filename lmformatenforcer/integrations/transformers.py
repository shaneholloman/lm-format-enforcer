from typing import Any, Callable, List, Tuple, Union
try:
    from transformers import AutoModelForCausalLM
    from transformers.generation.logits_process import LogitsWarper, PrefixConstrainedLogitsProcessor
    from transformers.tokenization_utils import PreTrainedTokenizerBase
except ImportError:
    raise ImportError('transformers is not installed. Please install it with "pip install transformers[torch]"')

try:
    import torch
except ImportError:
    raise ImportError('pytorch is not installed. See https://pytorch.org/get-started/locally/ for installation instructions."')

from ..characterlevelparser import CharacterLevelParser
from ..tokenenforcer import TokenEnforcer
from ..analyzer import FormatEnforcerAnalyzer

class LogitsSaverWarper(LogitsWarper):
    def __init__(self, analyzer: FormatEnforcerAnalyzer) -> None:
        self.analyzer = analyzer
        
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
        cpu_inputs = input_ids.tolist()
        cpu_scores = scores.tolist()
        for single_batch_inputs, single_batch_scores in zip(cpu_inputs, cpu_scores):
            self.analyzer.report_raw_logits(single_batch_inputs, single_batch_scores)
        return scores
    
class LogitsSaverManager:
    warper: LogitsSaverWarper

    def __init__(self, model: AutoModelForCausalLM, analyzer: FormatEnforcerAnalyzer):
        self.model = model
        self.warper = None
        self.old_warper = None
        self.analyzer = analyzer

    def replace_logits_warper(self, filter_func = None):
        self.old_warper = self.model._get_logits_warper

        def new_logits_warper(generation_config):
            warpers = self.old_warper(generation_config)
            self.warper = LogitsSaverWarper(self.analyzer)
            warpers.insert(0, self.warper)
            if filter_func is not None:
                processor = PrefixConstrainedLogitsProcessor(filter_func, 1)
                warpers.insert(1, processor)
            return warpers
        self.model._get_logits_warper = new_logits_warper

    def unreplace_logits_warper(self):
        self.model._get_logits_warper = self.old_warper

def build_regular_tokens_list(tokenizer: PreTrainedTokenizerBase) -> List[Tuple[int, str]]:
    token_0 = tokenizer.encode("0")[-1]
    regular_tokens = []
    for token_idx in range(tokenizer.vocab_size):
        if token_idx in tokenizer.all_special_ids:
            continue
        # We prepend token 0 and skip the first letter of the result to get a space if the token is a start word.
        decoded = tokenizer.decode([token_0, token_idx])[1:]
        regular_tokens.append((token_idx, decoded))
    return regular_tokens


class TransformersPrefixAllowedTokensFn:
    def __init__(self, token_enforcer: TokenEnforcer):
        self.token_enforcer = token_enforcer
        
    def __call__(self, batch_id: int, sent: torch.Tensor) -> List[int]:
        token_sequence = sent.tolist()
        return self.token_enforcer.get_allowed_tokens(token_sequence)


def build_transformers_prefix_allowed_tokens_fn(tokenizer: PreTrainedTokenizerBase, 
                                                character_level_parser: CharacterLevelParser) -> TransformersPrefixAllowedTokensFn:
    """Build the prefix allowed tokens function that transformers will use to filter the tokens generated by the model. The result
    can be passed to the prefix_allowed_tokens_fn parameter of the generate() method of transformers models or pipeline configurations."""
    regular_tokens = _build_regular_tokens_list(tokenizer)
    token_enforcer = TokenEnforcer(regular_tokens, character_level_parser, tokenizer.decode, tokenizer.eos_token_id)
    return TransformersPrefixAllowedTokensFn(token_enforcer)


def generate_enforced(model: AutoModelForCausalLM, 
                      tokenizer: PreTrainedTokenizerBase, 
                      character_level_parser: CharacterLevelParser, 
                      **kwargs: dict) -> Union[str, dict]:
    """Generate text from a model while enforcing a given format, generating enforcing diagnostic information. 
    This can be used instead of calling model.generate().
    If return_dict_in_generate and output_scores parameters are True, diagnostic information will be returned in the result.
    If you don't need this, consider using prefix_allowed_tokens_fn + build_transformers_prefix_allowed_tokens_fn() instead"""
    
    transformers_filter_allowed_tokens = build_transformers_prefix_allowed_tokens_fn(tokenizer, character_level_parser)
    
    is_multi_inputs = kwargs['input_ids'].shape[0] > 1
    is_multi_beams = kwargs.get('num_beams', 1) > 1
    support_diagnostics = not (is_multi_inputs or is_multi_beams)  # TODO: Support diagnostics in these cases as well.
    return_dict_in_generate = kwargs.get('return_dict_in_generate', False)
    output_scores = kwargs.get('output_scores', None)

    # We do some internals hacking in order to extract the data needed for diagnostics. If we weren't asked for them,
    # we are better off simply using prefix_allowed_tokens_fn parameter.
    should_run_in_advanced_mode = return_dict_in_generate and output_scores and support_diagnostics

    if should_run_in_advanced_mode:
        analyzer = FormatEnforcerAnalyzer(transformers_filter_allowed_tokens.token_enforcer)
        logits_saver = LogitsSaverManager(model, analyzer)
        logits_saver.replace_logits_warper(transformers_filter_allowed_tokens)
        generate_kwargs = kwargs
        
        try:
            output = model.generate(**generate_kwargs)
        finally:
            logits_saver.unreplace_logits_warper()

        df_dict = analyzer.generate_report_dict(output['sequences'][0].tolist())
        output.enforced_scores = df_dict
    else:
        output = model.generate(**kwargs, prefix_allowed_tokens_fn=transformers_filter_allowed_tokens)
    
    return output

__all__ = [
    'build_transformers_prefix_allowed_tokens_fn', 
    'generate_enforced',
    'build_regular_tokens_list'
]