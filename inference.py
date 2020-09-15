"""TTS Inference script."""

import configargparse
import logging
import os
import torch
import sys
from utils.util import set_deterministic_pytorch
from fastspeech import FeedForwardTransformer
from dataset.texts import phonemes_to_sequence
import time
from dataset.audio_processing import griffin_lim
import numpy as np
from utils.stft import STFT
from scipy.io.wavfile import write
from dataset.texts import valid_symbols
from utils.hparams import HParam, load_hparam_str
from dataset.texts.cleaners import english_cleaners, punctuation_removers
import matplotlib.pyplot as plt
from g2p_en import G2p

punctuations = '''!()[]{};:'"\<>./?@#^&_~'''

def synthesis(args, text, hp):
    """Decode with E2E-TTS model."""
    set_deterministic_pytorch(args)
    # read training config
    idim = hp.symbol_len
    odim = hp.num_mels
    model = FeedForwardTransformer(idim, odim, hp)
    print(model)

    if os.path.exists(args.path):
        print("\nSynthesis Session...\n")
        model.load_state_dict(torch.load(args.path), strict=False)
    else:
        print("Checkpoint not exixts")
        return None

    model.eval()

    # set torch device
    device = torch.device("cuda" if args.ngpu > 0 else "cpu")
    model = model.to(device)

    input = np.asarray(phonemes_to_sequence(text.split()))
    text = torch.LongTensor(input)
    text = text.cuda()
    # [num_char]

    with torch.no_grad():
        # decode and write
        idx = input[:5]
        start_time = time.time()
        print("text :", text.size())
        outs, probs, att_ws = model.inference(text, hp)
        print("Out size : ", outs.size())

        logging.info(
            "inference speed = %s msec / frame."
            % ((time.time() - start_time) / (int(outs.size(0)) * 1000))
        )
        if outs.size(0) == text.size(0) * args.maxlenratio:
            logging.warning("output length reaches maximum length .")

        print("mels", outs.size())
        mel = outs.cpu().numpy()  # [T_out, num_mel]
        print("numpy ", mel.shape)

        return mel


### for direct text/para input ###


g2p = G2p()


def plot_mel(mels):
    melspec = mels.reshape(1, 80, -1)
    plt.imshow(melspec.detach().cpu()[0], aspect="auto", origin="lower")
    plt.savefig("mel.png")

def punctuation_removers(text):
    
    no_punct = ""
    for char in text:
        if char not in punctuations:
            no_punct = no_punct + char
    return no_punct

def preprocess(text):

    # input - line of text
    # output - list of phonemes
    str1 = " "
    clean_content = english_cleaners(text)
    phonemes = g2p_m(clean_content)
    pau_index = []
    phonemes = ["" if x == " " else x for x in phonemes]
    phonemes = ["pau" if x == "," else x for x in phonemes]
    phonemes = ["pau" if x == "." else x for x in phonemes]
    phonemes.append("sil")
    phonemes = str1.join(phonemes)
    print(phonemes.split(), "Input Phonemes")
    for ind in range(0, len(phonemes.split())):
        if phonemes.split()[ind] == "pau":
            pau_index.append(ind)
    #print(pau_index, "pau_index")
    return phonemes, pau_index


def process_paragraph(para):
    # input - paragraph with lines seperated by "." the para should end with a full stop.
    # input can have multiple spaces in between
    # can have puncuations, special characters
    # output - list with each item as lines of paragraph seperated by suitable padding
    # 
    text = []
    for lines in para.split("."):
        lines = " ".join(lines.split())
        lines = punctuation_removers(lines)
        text.append(lines.strip() + ".")
    
    return text[:-1]

def g2p_m(input_sentence, sp_char="*"):
    input_phonemes = []
    pau_index = []
    words = input_sentence.split()
    for word in words:
        if word == sp_char:
            phon = "pau"
            input_phonemes.append(phon)
        else:
            phon = g2p(word)
            for ph in phon:
                input_phonemes.append(ph)
    return input_phonemes


def synth(text, model, hp, pau_index):
    """Decode with E2E-TTS model."""

    print("TTS synthesis")

    model.eval()
    # set torch device
    device = torch.device("cuda" if hp.train.ngpu > 0 else "cpu")
    model = model.to(device)
    #print(len(text.split()), "INput Text before passing to phonemenes to sequence")

    input = np.asarray(phonemes_to_sequence(text))
    #print(len(input), "Input")

    text = torch.LongTensor(input)
    text = text.to(device)

    with torch.no_grad():
        print("predicting")
        #print(text,"input text")
        outs = model.inference_m(text, pau_index)  # model(text) for jit script
        mel = outs
    return mel


def main(args):
    """Run deocding."""
    para_mel = []
    parser = get_parser()
    args = parser.parse_args(args)

    logging.info("python path = " + os.environ.get("PYTHONPATH", "(None)"))

    print("Text : ", args.text)
    print("Checkpoint : ", args.checkpoint_path)
    if os.path.exists(args.checkpoint_path):
        checkpoint = torch.load(args.checkpoint_path)
    else:
        logging.info("Checkpoint not exixts")
        return None

    if args.config is not None:
        hp = HParam(args.config)
    else:
        hp = load_hparam_str(checkpoint["hp_str"])

    idim = len(valid_symbols)
    odim = hp.audio.num_mels
    model = FeedForwardTransformer(
        idim, odim, hp
    )  # torch.jit.load("./etc/fastspeech_scrip_new.pt")

    os.makedirs(args.out, exist_ok=True)
    if args.old_model:
        logging.info("\nSynthesis Session...\n")
        model.load_state_dict(checkpoint, strict=False)
    else:
        checkpoint = torch.load(args.checkpoint_path)
        model.load_state_dict(checkpoint["model"])

    text = process_paragraph(args.text)

    for i in range(0, len(text)):
        txt, pau_index = preprocess(text[i])
        audio = synth(txt, model, hp, pau_index)
        m = audio.T
        para_mel.append(m)

    m = torch.cat(para_mel, dim=1)
    np.save("mel.npy", m.cpu().numpy())
    plot_mel(m)

    if args.vocoder == 1:
        print("Using WaveGlow Vocoder")
        m = m.unsqueeze(0)
        print("Mel shape: ", m.shape)
        waveglow = torch.hub.load('nvidia/DeepLearningExamples:torchhub', 'nvidia_waveglow')
        waveglow = waveglow.remove_weightnorm(waveglow)
        vocoder = waveglow.to('cuda')
        #vocoder = torch.hub.load("seungwonpark/melgan", "melgan")
        vocoder.eval()
        if torch.cuda.is_available():
            vocoder = vocoder.cuda()
            mel = m.cuda()

        with torch.no_grad():
            wav = vocoder.infer(
                mel
            )  # mel ---> batch, num_mels, frames [1, 80, 234]
            wav = wav.cpu().float().numpy()
        save_path = "{}/test_tts_waveglow.wav".format(args.out)
        print(wav.shape)
        write(save_path, hp.audio.sample_rate, wav.T)
            
    if args.vocoder == 0:
        print("Using MelGan Vocoder")
        m = m.unsqueeze(0)
        print("Mel shape: ", m.shape)
        vocoder = torch.hub.load("seungwonpark/melgan", "melgan")
        vocoder.eval()
        if torch.cuda.is_available():
            vocoder = vocoder.cuda()
            mel = m.cuda()

        with torch.no_grad():
            wav = vocoder.inference(
                mel
            )  # mel ---> batch, num_mels, frames [1, 80, 234]
            wav = wav.cpu().float().numpy()
        save_path = "{}/test_tts_melgan.wav".format(args.out)
        write(save_path, hp.audio.sample_rate, wav.astype("int16"))
        
    #else:
     #   stft = STFT(filter_length=1024, hop_length=256, win_length=1024)
    #    print(m.size())
    #    m = m.unsqueeze(0)
    #    wav = griffin_lim(m, stft, 30)
    #    wav = wav[0].data.cpu().numpy()
    #save_path = "{}/test_tts_waveglow.wav".format(args.out)
    #print(wav.shape)
    #write(save_path, hp.audio.sample_rate, wav.T) #astype("int16"))
    
# NOTE: you need this func to generate our sphinx doc
def get_parser():
    """Get parser of decoding arguments."""
    parser = configargparse.ArgumentParser(
        description="Synthesize speech from text using a TTS model on one CPU",
        config_file_parser_class=configargparse.YAMLConfigFileParser,
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
    )
    # general configuration

    parser.add_argument(
        "-c", "--config", type=str, required=True, help="yaml file for configuration"
    )
    parser.add_argument(
        "-p",
        "--checkpoint_path",
        type=str,
        default=None,
        help="path of checkpoint pt file to resume training",
    )
    parser.add_argument("--out", type=str, required=True, help="Output filename")
    parser.add_argument(
        "-o", "--old_model", action="store_true", help="Resume Old model "
    )
    # task related
    parser.add_argument(
        "--text", type=str, required=True, help="Filename of train label data (json)"
    )
    parser.add_argument(
        "--pad", default=0, type=int, help="padd value at the end of each sentence"
    )
    parser.add_argument("-v", "--vocoder", type = int, required = True, help = "0: Melgan, 1: WaveGlow")
    
    return parser


if __name__ == "__main__":
    print("Starting")
    main(sys.argv[1:])
