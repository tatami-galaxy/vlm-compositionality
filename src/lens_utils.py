"""logit lens and tuned lens functions"""

from PIL import Image
import numpy as np
import torch
from transformers.generation.logits_process import TopKLogitsWarper


def llava_logit_lens(inputs, model, outputs, output_type=None, logit_topk=50, norm=False):
    """get logit lens distribution over vocab"""

    assert outputs['hidden_states'] is not None
    assert output_type is not None

    input_ids = inputs['input_ids']
    # https://github.com/huggingface/transformers/blob/main/src/transformers/generation/logits_process.py#L484
    logits_warper = TopKLogitsWarper(top_k=logit_topk, filter_value=float("-inf"))
    image_token_id = model.config.image_token_index

    if output_type == 'generate':
        # first forward pass
        hidden_states = torch.stack(outputs.hidden_states[0])
    elif output_type == 'forward':
        hidden_states = torch.stack(outputs.hidden_states)
    
    with torch.inference_mode():
        
        # norm
        if norm:
            hidden_states[:-1, :, :, :] = model.language_model.model.norm(hidden_states[:-1, :, :, :])

        # unembedding
        curr_layer_logits = model.language_model.lm_head(hidden_states).cpu().float() # -> TODO: stop here, use only logit
        logit_scores = torch.nn.functional.log_softmax(curr_layer_logits, dim=-1)
        
        # remove all tokens with a probability less than the last token of the top-k
        # input_ids not used by the __call__ method
        logit_scores = logits_warper(input_ids, logit_scores)
        # softmax -> re-distribute probability mass
        softmax_probs = torch.nn.functional.softmax(logit_scores, dim=-1)
        softmax_probs = softmax_probs.detach().cpu().numpy()

        # get scores of image embeddings
        image_token_index = input_ids.tolist()[0].index(image_token_id)
        softmax_probs = softmax_probs[
            :, :, image_token_index : image_token_index + (24 * 24) # 576 -> compute differently?
        ]
        
        # transpose to (vocab_dim, num_layers, num_tokens, num_beams)
        softmax_probs = softmax_probs.transpose(3, 0, 2, 1)

        # maximum over all beams
        # vocab_dim, num_layers, num_tokens
        softmax_probs = softmax_probs.max(axis=3)

        return softmax_probs
    

# TODO: non zero values, topk, what else?
def get_mask_from_lens(
        softmax_probs,
        token, processor,
        num_patches,
        width,
        height,
        mask_type='topk',
        mask_topk=10000
    ):

    class_token_indices = processor.tokenizer.encode(token)[1:]
    # max across subwords, then max across layers
    #softmax_probs = softmax_probs[class_token_indices].max(axis=0).max(axis=0)
    # mean across subwords, then max across layers
    softmax_probs = np.mean(softmax_probs[class_token_indices], axis=0).max(axis=0)
    segmentation = softmax_probs.reshape(num_patches, num_patches).astype(float)
    # bilinear interpolation of lens values
    segmentation_resized = (np.array(Image.fromarray(segmentation).resize((width, height), Image.BILINEAR)))
    # get zero mask
    mask = np.zeros(segmentation_resized.shape)

    if mask_type == 'nonzero':
        # consider nonzero entries
        non_zeros = np.nonzero(segmentation_resized)
        # set non zero segmentation value positions to 1 in mask
        # TODO: vectorize
        for n in range(len(non_zeros[0])):
            mask[non_zeros[0][n], non_zeros[1][n]] = 1

    elif mask_type == 'topk':
        # topk values
        # https://stackoverflow.com/questions/57103908/finding-indices-of-k-top-values-in-a-2d-array-matrix
        max_k = np.c_[np.unravel_index(np.argpartition(segmentation_resized.ravel(),-mask_topk)[-mask_topk:],segmentation_resized.shape)]
        # TODO: vectorize
        # set non zero segmentation value positions to 1 in mask
        for k in range(mask_topk):
            mask[max_k[k][0], max_k[k][1]] = 1

    else:
        raise ValueError("Unknown mask type")

    return mask