"""# Imports"""

import GPUtil
import copy
import humanize
import math
import psutil
import sys

import pandas as pd
import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from transformers import DistilBertTokenizer, DistilBertForMaskedLM, DistilBertConfig, PreTrainedTokenizer

if torch.cuda.is_available():
    dev = "cuda:0"
else:
    dev = "cpu"
device = torch.device(dev)
print("using device: ", dev)


# Define function
def mem_report():
    print("CPU RAM Free: " + humanize.naturalsize(psutil.virtual_memory().available))

    GPUs = GPUtil.getGPUs()
    for i, gpu in enumerate(GPUs):
        print('GPU {:d} ... Mem Free: {:.0f}MB / {:.0f}MB | Utilization {:3.0f}%'.format(i, gpu.memoryFree,
                                                                                         gpu.memoryTotal,
                                                                                         gpu.memoryUtil * 100))


mem_report()

"""# Hyperparameters"""

# hyperparameters
DEBUG = False
CONTINUE_TRAIN = False
BATCH_SIZE = 8
MAX_LENGTH = 16  # max text length
LEARNING_RATE = 1e-4
END_LEARNING_RATE = 5e-5  # learning rate is reduced to end_learning_rate


def cosine_annealing():
    sub_epoch = 5
    x = torch.arange(0, sub_epoch)
    x = END_LEARNING_RATE + (LEARNING_RATE - END_LEARNING_RATE) * (1 + torch.cos(x / sub_epoch * math.pi)) / 2
    return x.repeat((3,))


SCHEDULER = torch.linspace
TRAIN_SET_RATIO = 0.8
EARLY_STOP_RATIO = 1.05
EPOCH_NUM = 5
DYNAMIC_ROUNDING_WEIGHT = -1  # weight of rounding term with respect to x_t loss, <0 means not using
ROUNDING_WEIGHT = 0.5  # weight of rounding term, the probability of regenerated sequence, not used if using dynamic rounding


def series_sum_sample_mean(x_hat, x):
    return (x_hat - x).abs().sum(dim=1).mean()


def series_sum(x_hat, x):
    return (x_hat - x).abs().sum() / BATCH_SIZE / 768 / 100


def mse_series_mean(x_hat, x):
    return ((x_hat - x) ** 2).sum(dim=[-2, -1]).sqrt().mean()


def mse_series_sum(x_hat, x):
    return ((x_hat - x) ** 2).sum(dim=[-2, -1]).sqrt().sum() / BATCH_SIZE


LOSS_FUNC = series_sum_sample_mean
CLIP_ADDING_METHOD = "concat"  # CLIP feature are appended to sequence of word embedding
CLASSIFIER_FREE_WEIGHT = 0
CLASSIFIER_FREE_PROB = 0.2
TRAIN_EMBEDDING = False  # if clip-vit-base-patch32-local use pretrained distilbert embedding, or learn a 16 embedding for each word and project to 768 before pass to bert
if TRAIN_EMBEDDING:
    IN_CHANNEL = 16
else:
    IN_CHANNEL = 768

# diffusion hyperparameter
BETA_MIN = 0.0001
BETA_MAX = 0.02
STEP_TOT = 1000  # total noise adding steps
COSIN_SCHEDULE = True  # if alpha sequence is scheduled in cosin instead of linear patten
SAMPLE_SIZE = 100  # number of sample steps in each diffuse sequence
X_0_PREDICTION = True  # if clip-vit-base-patch32-local predicts x_0 or x_{t-1}
X_T_STEP_INTERVAL = 100
USE_X_T_LOSS = True
USE_X_1_LOSS = True  # if using x_1 loss
USE_PROB_LOSS = True  # if using prob loss

MODEL_NAME = f"epoch{EPOCH_NUM}_loss{LOSS_FUNC.__name__}_lr{'%.0E' % LEARNING_RATE}-{'%.0E' % END_LEARNING_RATE}_scheduler{SCHEDULER.__name__}_round{'%.0E' % ROUNDING_WEIGHT}_dynamic{DYNAMIC_ROUNDING_WEIGHT}\
_clip{CLIP_ADDING_METHOD}_class_weight{'%.0E' % CLASSIFIER_FREE_WEIGHT}_class_prob{'%.0E' % CLASSIFIER_FREE_PROB}_train-embed{TRAIN_EMBEDDING}\
_samplesize{SAMPLE_SIZE}_x_0_predict{X_0_PREDICTION}_X_INTERVAL{X_T_STEP_INTERVAL}_use_x_t{USE_X_T_LOSS}_use_x_1{USE_X_1_LOSS}_use_prob{USE_PROB_LOSS}"
print(f"trial name: {MODEL_NAME}")

"""# Define Dataset"""

flickr8k_image = torch.load("DataSet-CLIP-freature/building/image_all_final.pickle").to(device).detach()
flickr8k_text = torch.load("DataSet-CLIP-freature/building/text_all_final.pickle").to(device).detach()
flickr30k_image = torch.load("DataSet-CLIP-freature/building/flickr30k_clip_image.pickle").to(device).detach()
flickr30k_text = torch.load("DataSet-CLIP-freature/building/flickr30k_clip_text.pickle").to(device).detach()
image_set = torch.vstack([flickr8k_image, flickr30k_image])
text_set = torch.vstack([flickr8k_text, flickr30k_text])

from spacy.lang.en import English
from collections import Counter
import itertools

captions = pd.read_csv("DataSet-CLIP-freature/DataSet/captions.txt")["caption"]
nlp = English()

sentence_lst = []

for sentences in captions:
    word_lst = [x.text.lower() for x in nlp.tokenizer(sentences)]
    spl = [[]]
    for x, y in itertools.groupby(word_lst, lambda z: z == '.'):
        spl[-1].extend(y)
        if x: spl.append([])
    sentence_lst.extend(spl[:-1])

counter = Counter()
for input_ids in sentence_lst:
    counter.update(input_ids)
vocab_dict = {'START': 0, 'END': 1, 'UNK': 2, 'PAD': 3}
for k, v in counter.items():
    if v > 10:
        vocab_dict[k] = len(vocab_dict)


class DictTokenizer():
    def __init__(self, dictionary) -> None:
        self.dictionary = dictionary

    def __getitem__(self, i):
        return self.dictionary[i]

    def decode(self, index):
        return " ".join([list(self.dictionary.keys())[list(self.dictionary.values()).index(i.item())] for i in index])


class FlickrCLIPDataset(torch.utils.data.Dataset):
    def __init__(self, captions, images, tokenizer) -> None:
        images.name = "image"
        captions.name = "caption"
        self.data = pd.concat([images, captions], axis=1)
        self.tokenizer = tokenizer

        self.train_dataset = torch.utils.data.TensorDataset(image_set.to(device), text_set.to(device))

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        image_clip, text_clip = self.train_dataset[idx]
        if isinstance(self.tokenizer, PreTrainedTokenizer):
            tokens = self.tokenizer(text=self.data.loc[idx]["caption"], return_tensors="pt", padding='max_length',
                                    truncation=True, max_length=MAX_LENGTH)
        else:
            ids = [0] + [vocab_dict.get(x, vocab_dict['UNK']) for x in
                         self.data.loc[idx]["caption"][:MAX_LENGTH - 2]] + [1]
            pad_length = max(0, MAX_LENGTH - len(ids))
            tokens = dict()
            tokens["input_ids"] = torch.tensor(ids + [vocab_dict['UNK']] * pad_length)
            tokens["attention_mask"] = torch.tensor([1] * len(ids) + [0] * pad_length)

        return {
            "image_clip": image_clip,
            "text_clip": text_clip,
            "input_ids": tokens["input_ids"].squeeze().to(device),
            "attention_mask": tokens["attention_mask"].squeeze().to(device),
            "text": self.data.loc[idx]["caption"],
            "image": self.data.loc[idx]["image"]
        }


# TODO: COCO dataset

if TRAIN_EMBEDDING:
    tokenizer = DictTokenizer(vocab_dict)
    VOCAB_SIZE = len(vocab_dict)
else:
    tokenizer = DistilBertTokenizer.from_pretrained("./tokenizers/distilbert-base-uncased-local/",
                                                    local_files_only=True)
    VOCAB_SIZE = tokenizer.vocab_size

dataset = FlickrCLIPDataset(
    pd.concat([pd.read_csv("DataSet/captions.txt")["caption"],
               pd.read_csv("./flickr30k/captions.csv", sep='|')["caption"]], ignore_index=True),

    pd.concat([pd.read_csv("DataSet/captions.txt")["image"],
               pd.read_csv("./flickr30k/captions.csv", sep='|')["image_name"]], ignore_index=True),
    tokenizer)
if CONTINUE_TRAIN:
    val_set = torch.load(f"{MODEL_NAME}.valset")
    train_set = torch.utils.data.Subset(dataset, list(set(range(len(dataset))) - set(val_set.indices)))
else:
    train_len = int(len(dataset) * TRAIN_SET_RATIO)
    train_set, val_set = torch.utils.data.random_split(dataset, [train_len, len(dataset) - train_len])
train_loader = DataLoader(train_set, shuffle=True, batch_size=BATCH_SIZE, drop_last=True)
val_loader = DataLoader(val_set, shuffle=False, batch_size=BATCH_SIZE, drop_last=True)

mem_report()

"""# Model, trainer and loss function"""


class DistilBertModel(nn.Module):
    def __init__(self, embedding=None, projection=None, config=None) -> None:
        """
        inputs:
          embedding: clip embedding module
          config
        """
        super().__init__()

        self.model = DistilBertForMaskedLM(config).to(device)

        if TRAIN_EMBEDDING:
            self.embedding = nn.Embedding(VOCAB_SIZE, IN_CHANNEL, device=device).requires_grad_(True)
            self.lm_head = nn.Linear(IN_CHANNEL, VOCAB_SIZE, bias=False, device=device).requires_grad_(True)

            self.input_projection = nn.Linear(IN_CHANNEL, 768, device=device).requires_grad_(True)
            self.output_projection = nn.Linear(768, IN_CHANNEL, device=device).requires_grad_(True)
        else:
            self.embedding = copy.deepcopy(embedding.requires_grad_(False))
            self.lm_head = copy.deepcopy(projection.requires_grad_(False))
            self.lm_head.bias.data = torch.zeros(self.lm_head.bias.data.shape, device=device).requires_grad_(False)

        self.model.set_input_embeddings(nn.Sequential())
        self.model.set_output_embeddings(nn.Sequential())

        self.image_linear = nn.Linear(512, 768, device=device)
        self.text_linear = nn.Linear(512, 768, device=device)

        if CLIP_ADDING_METHOD == "concat":
            self.segment_embedding = nn.Embedding(2, 768, device=device)

    def parameters(self):
        base_list = list(self.model.parameters()) + list(self.image_linear.parameters()) + list(
            self.text_linear.parameters())
        if TRAIN_EMBEDDING:
            base_list += list(self.embedding.parameters()) + list(self.lm_head.parameters()) \
                         + list(self.input_projection.parameters()) + list(self.output_projection.parameters())

        if CLIP_ADDING_METHOD == "concat":
            return base_list + list(self.segment_embedding.parameters())
        elif CLIP_ADDING_METHOD == "add":
            return base_list
        else:
            raise NotImplementedError(CLIP_ADDING_METHOD)

    def forward(self, x, image_clip, text_clip, mask, concat_mask):
        """
        input:
            x: [x_t ... x_t], shape: [sample_size * batch_size, seq_len, IN_CHANNEL]
            image_clip, text_clip shape: [sample_size * batch_size, 1, clip_dim]
            mask shape: [sample_size * batch_size, seq_len]

        return
            vocab_out, shape: [sample_size * batch_size, seq_len, vocab_size]
            feature_out, shape: [sample_size * batch_size, seq_len, IN_CHANNEL]
        """
        sample_batch_multi, _, _ = x.shape

        assert x.shape == (sample_batch_multi, MAX_LENGTH, IN_CHANNEL)
        assert image_clip.shape == text_clip.shape == (sample_batch_multi, 1, 512)
        assert mask.shape == (sample_batch_multi, MAX_LENGTH)
        assert concat_mask.shape == (sample_batch_multi, 2)

        # mask of which sample is classifier free guided, true if guided
        guidance_sample_index = (concat_mask[:, 1] == 1)

        if TRAIN_EMBEDDING:
            x = self.input_projection(x)

        if CLIP_ADDING_METHOD == "concat":
            classifier_guided_mask = torch.hstack(
                [mask, torch.tensor([1, 1], device=device).repeat(sample_batch_multi, 1)])
            non_classifier_mask = torch.hstack(
                [mask, torch.tensor([1, 0], device=device).repeat(sample_batch_multi, 1)])

            x = torch.hstack([x, self.image_linear(image_clip), self.text_linear(text_clip)])
            x = x + self.segment_embedding(torch.tensor([0] * MAX_LENGTH + [1] * 2, device=device))

            classifier_guided_x = non_classifier_x = x
        elif CLIP_ADDING_METHOD == "add":
            classifier_guided_mask = non_classifier_mask = mask

            non_classifier_x = x + self.image_linear(image_clip)
            classifier_guided_x = non_classifier_x + self.text_linear(text_clip)
        else:
            raise NotImplementedError(CLIP_ADDING_METHOD)

        # no classifier guidance part
        x_out = self.model(non_classifier_x, non_classifier_mask)[0]
        if CLASSIFIER_FREE_WEIGHT > 0 and not guidance_sample_index.sum() == 0:
            # classifier guided
            x_out[guidance_sample_index] = \
                (1 + CLASSIFIER_FREE_WEIGHT) * \
                self.model(classifier_guided_x[guidance_sample_index], classifier_guided_mask[guidance_sample_index])[0] \
                - CLASSIFIER_FREE_WEIGHT * x_out[guidance_sample_index]

        if TRAIN_EMBEDDING:
            x_out = self.output_projection(x_out)

        assert x_out.shape == (sample_batch_multi, non_classifier_mask.shape[-1], IN_CHANNEL)
        return self.lm_head(x_out[:, :MAX_LENGTH, :]), x_out


if TRAIN_EMBEDDING:
    configuration = DistilBertConfig()
    model = DistilBertModel(config=configuration)
else:
    origin = DistilBertForMaskedLM.from_pretrained("./models/distilbert-base-uncased-local", local_files_only=True).to(
        device)
    configuration = DistilBertConfig()
    model = DistilBertModel(origin.get_input_embeddings(), origin.get_output_embeddings(), config=configuration)

# parameter only include clip-vit-base-patch32-local, no embedding layer
trainer = optim.AdamW(model.parameters(), lr=LEARNING_RATE)

if COSIN_SCHEDULE:
    def scheduler(t):
        s = 0.008  # smalle value prevent beta_t too small, from Improved DDPM paper
        return torch.cos(math.pi / 2 * (t / STEP_TOT + s) / (1 + s)) ** 2


    ts = torch.arange(STEP_TOT).to(device)
    alpha_cumprod = scheduler(ts) / scheduler(torch.zeros(1, device=device))
else:
    betas = torch.hstack([torch.zeros(1), torch.linspace(BETA_MIN, BETA_MAX, STEP_TOT)]).to(device)
    alphas = 1 - betas
    alpha_cumprod = torch.cumprod(alphas[:-1], 0)


def diffuse_t(x, t):
    """
    input:
        x_shape: [batch_size, seq_len, IN_CHANNEL]
        t shape: [sample num]
            NOTE: not necessary have hyperparameter sample_size number of element, to allow single diffuse generation

    return shape [sample_num * batch_size, seq_len, IN_CHANNEL]
    """
    batch_size, seq_len, _ = x.shape
    sample_shape = (t.numel(), *(1,) * len(x.shape))

    noise = torch.normal(0, 1, x.shape).to(device)
    mean = torch.sqrt(alpha_cumprod[t].reshape(sample_shape)) * x
    epsilon = noise * torch.sqrt(1 - alpha_cumprod[t]).reshape(sample_shape)
    return (mean + epsilon).reshape((t.numel() * batch_size, seq_len, IN_CHANNEL))


def generate_diffuse_pair(x_0, t, t_next=None):
    """
    input:
        x_0 shape: [batch_size, seq_len, IN_CHANNEL],
        t shape: [sample_num]
            NOTE: not necessary have hyperparameter sample_size number of element, to allow single diffuse generation

    return (net input, net target)
        net input shape: [sample_num * batch_size, seq_len, IN_CHANNEL]
        net target shape: if t_next is None then [batch_size, seq_len, IN_CHANNEL] else [sample_num * batch_size, seq_len, IN_CHANNEL]
    """
    if X_0_PREDICTION:
        # predict x_0
        return (diffuse_t(x_0, t), x_0)

    # predict x_{t_next}
    return (diffuse_t(x_0, t), diffuse_t(x_0, t_next))


def loss(model, x_t, x_1, x_tgt, x_0, image_clip, text_clip, mask, idx, loss_func):
    """
    input:
        clip-vit-base-patch32-local,
        x_t, x_tgt shape: [sample_num * batch_size, seq_len, IN_CHANNEL]
            NOTE: x_tgt only used when X_0_PREDICTION is False
        x_1, x_0 shape: [batch_size, seq_len, IN_CHANNEL]
        image_clip, text_clip shape: [batch_size, clip_dim]
        mask shape: [batch_size, seq_len]
        idx shape: [batch_size, seq_len]
        loss_func

    return triple loss terms
    """
    assert x_t.shape == (SAMPLE_SIZE * BATCH_SIZE, MAX_LENGTH, IN_CHANNEL)
    assert x_1.shape == x_0.shape == (BATCH_SIZE, MAX_LENGTH, IN_CHANNEL)
    assert image_clip.shape == text_clip.shape == (BATCH_SIZE, 512)
    assert mask.shape == (BATCH_SIZE, MAX_LENGTH)
    assert idx.shape == (BATCH_SIZE, MAX_LENGTH)

    repeat_shape = (SAMPLE_SIZE, *(1,) * (len(x_t.shape) - 1))
    image_clip = image_clip.unsqueeze(1)  # shape [ batch_size, 1, clip_dim]
    text_clip = text_clip.unsqueeze(1)  # shape same as above

    if CLASSIFIER_FREE_WEIGHT > 0:
        classifier_mask = (torch.rand((SAMPLE_SIZE * BATCH_SIZE, 1)) > CLASSIFIER_FREE_PROB).type(torch.float32).to(
            device)
        classifier_mask[0] = 0
        classifier_mask[1] = 1  # prevent no sample or all sample use classifier
        concat_mask = torch.hstack([torch.ones((SAMPLE_SIZE * BATCH_SIZE, 1), device=device), classifier_mask])
    else:
        concat_mask = torch.tensor([1, 0], device=device).repeat((SAMPLE_SIZE * BATCH_SIZE, 1))

    # x_t restore loss
    x_t_prob, x_t_hidden = model(x_t, image_clip.repeat(repeat_shape), text_clip.repeat(repeat_shape),
                                 mask.repeat((SAMPLE_SIZE, 1)), concat_mask)
    if USE_X_T_LOSS:
        if X_0_PREDICTION:
            x_t_loss = loss_func(x_t_hidden[:, :MAX_LENGTH, :], x_0.repeat(repeat_shape))
        else:
            assert x_tgt.shape == x_t.shape
            x_t_loss = loss_func(x_t_hidden[:, :MAX_LENGTH, :], x_tgt)
    else:
        x_t_loss = 0

    # x_1 restore loss
    x_1_prob, x_1_hidden = model(x_1, image_clip, text_clip, mask,
                                 torch.tensor([1, 0], device=device).repeat((BATCH_SIZE, 1)))
    if USE_X_1_LOSS:
        x_1_loss = loss_func(x_1_hidden[:, :MAX_LENGTH, :], x_0)
    else:
        x_1_loss = 0

    if USE_PROB_LOSS:
        # output sequence probability loss, applied to both x_1 and x_t restore
        idx = idx.unsqueeze(dim=-1)
        if LOSS_FUNC == series_sum_sample_mean or LOSS_FUNC == mse_series_mean:
            x_t_prob_loss = -(nn.functional.softmax(x_t_prob, dim=-1)).gather(-1, idx.repeat(repeat_shape)).log().sum(
                dim=1).mean()
            x_1_prob_loss = -(nn.functional.softmax(x_1_prob, dim=-1)).gather(-1, idx).log().sum(dim=1).mean()
        else:
            x_t_prob_loss = -(nn.functional.softmax(x_t_prob, dim=-1)).gather(-1, idx.repeat(
                repeat_shape)).log().sum() / BATCH_SIZE
            x_1_prob_loss = -(nn.functional.softmax(x_1_prob, dim=-1)).gather(-1, idx).log().sum() / BATCH_SIZE
    else:
        x_t_prob_loss = 0
        x_1_prob_loss = 0

    return x_t_loss, x_1_loss, ROUNDING_WEIGHT * (x_t_prob_loss + x_1_prob_loss)


mem_report()

"""# Training"""

if SCHEDULER == torch.linspace:
    lrs = SCHEDULER(LEARNING_RATE, END_LEARNING_RATE, EPOCH_NUM)
elif SCHEDULER == torch.logspace:
    lrs = SCHEDULER(torch.tensor([LEARNING_RATE]).log10().item(), torch.tensor([END_LEARNING_RATE]).log10().item(),
                    EPOCH_NUM)
elif SCHEDULER == cosine_annealing:
    lrs = SCHEDULER()


def train_func(model, trainer, x, train=True):
    x_0 = model.embedding(x["input_ids"])
    repeat_shape = (SAMPLE_SIZE, *(1,) * (len(x_0.shape) - 1))
    t = torch.randint(0, STEP_TOT, repeat_shape, device=device)

    if X_0_PREDICTION:
        x_t = diffuse_t(x_0, t)
        x_tgt = None
    else:
        x_t, x_tgt = generate_diffuse_pair(x_0, t, torch.max(t - X_T_STEP_INTERVAL,
                                                             torch.zeros(t.shape, device=device, dtype=torch.int64)))
    x_1 = diffuse_t(x_0, torch.ones(1, dtype=torch.int64, device=device))

    if train:
        trainer.zero_grad()
    x_t_loss, x_1_loss, prob_loss = loss(
        model,
        x_t, x_1, x_tgt, x_0,
        x["image_clip"], x["text_clip"],
        x["attention_mask"],
        x["input_ids"],
        LOSS_FUNC
    )

    l = x_t_loss + x_1_loss + prob_loss
    if train:
        l.backward()
        trainer.step()

    return l, x_t_loss, x_1_loss, prob_loss


def validate(model):
    val_acc_x_t = 0
    val_acc_x_1 = 0
    val_acc_prob = 0
    model.eval()
    with torch.no_grad():
        for batch_num, x in enumerate(val_loader):
            _, x_t_loss, x_1_loss, prob_loss = train_func(model, trainer, x, train=False)
            val_acc_x_t += x_t_loss
            val_acc_x_1 += x_1_loss
            val_acc_prob += prob_loss
    model.train()

    return val_acc_x_t / len(val_loader), val_acc_x_1 / len(val_loader), val_acc_prob / len(val_loader),


# training

if CONTINUE_TRAIN:
    model = torch.load(f"{MODEL_NAME}.pickle").to(device)
    # clip-vit-base-patch32-local.clip-vit-base-patch32-local.add_module("activation", activations.GELUActivation())
    trainer = optim.AdamW(model.parameters(), lr=LEARNING_RATE)
summary = open(f"{MODEL_NAME}.txt", "a")
# summary = sys.stdout

early_stopped = False
model.train()
print("start training")
for epoch in range(EPOCH_NUM):
    acc_x_t = 0
    acc_x_1 = 0
    acc_prob = 0
    acc_l = 0
    if not END_LEARNING_RATE == LEARNING_RATE:
        for g in trainer.param_groups:
            g['lr'] = lrs[epoch]
    for batch_num, x in enumerate(train_loader):

        l, x_t_loss, x_1_loss, prob_loss = train_func(model, trainer, x)

        acc_x_t += x_t_loss
        acc_x_1 += x_1_loss
        acc_prob += prob_loss
        acc_l += l

        if DYNAMIC_ROUNDING_WEIGHT > 0:
            ROUNDING_WEIGHT = ((acc_x_t + acc_x_1) / acc_prob).detach() * DYNAMIC_ROUNDING_WEIGHT
        if DEBUG:
            break

    val_x_t, val_x_1, val_prob = validate(model)
    if val_x_t + val_x_1 + val_prob > EARLY_STOP_RATIO * acc_l / len(train_loader):
        if not early_stopped:
            summary.write("early stop! \n")
            torch.save(model.cpu(), f"{MODEL_NAME}.pickle")
            model = model.to(device)
        early_stopped = True
    summary.write(
        f"epoch {epoch} average x_t_loss, x_1_loss, prob_loss, val losses: {acc_x_t / len(train_loader)}, {acc_x_1 / len(train_loader)}, {acc_prob / len(train_loader)}, {val_x_t}, {val_x_1}, {val_prob}\n")

    if DEBUG:
        break

if not early_stopped:
    torch.save(model.cpu(), f"{MODEL_NAME}.pickle")
    model = model.to(device)

mem_report()

"""# Evaluate"""

# summary = sys.stdout

# trial on inference
model = torch.load(
    f"{MODEL_NAME}.pickle",
).to(device)
# clip-vit-base-patch32-local.clip-vit-base-patch32-local.add_module("activation", activations.GELUActivation())
model.eval()
with torch.no_grad():
    idx = 0
    summary.write(f"origin text: {val_set[idx]['text']}\n")

    sample = val_set[idx]
    image_clip = sample["image_clip"][None, None, :]
    text_clip = sample["text_clip"][None, None, :]

    x_0 = model.embedding(sample["input_ids"].unsqueeze(0))
    t = 999
    summary.write(f"t = {t}\n")
    x_t = diffuse_t(x_0, torch.tensor([t], dtype=torch.int64, device=device))
    mask = sample["attention_mask"].unsqueeze(0)

    # multi-step inference
    restored = x_t
    for i in range(10):
        out, restored = model(restored[:, :MAX_LENGTH, :], image_clip, text_clip, mask,
                              torch.tensor([1, 0], device=device).repeat(mask.shape[0], 1))
        summary.write(f"inferred: {dataset.tokenizer.decode(out.argmax(dim=-1)[0])}\n")

    # effectiveness of clip-vit-base-patch32-local on large t
    summary.write("text t effectiveness\n")
    for i in range(1, STEP_TOT, 100):
        x_t = diffuse_t(x_0, torch.tensor([i], dtype=torch.int64, device=device))
        out, _ = model(x_t, image_clip, text_clip, mask, torch.tensor([1, 0], device=device).repeat(mask.shape[0], 1))

        summary.write(f"t: {i} restore: {dataset.tokenizer.decode(out.argmax(dim=-1)[0])}\n")

from torchmetrics import BLEUScore

metric = BLEUScore()
acc_bleu = 0
with torch.no_grad():
    for j, x in enumerate(val_loader):

        restored = torch.randn((x["input_ids"].shape[0], MAX_LENGTH + 2, IN_CHANNEL), device=device)

        # each prediction involves multiple generation steps
        for i in range(5):
            out, restored = model(restored[:, :MAX_LENGTH, :], x["image_clip"].unsqueeze(1),
                                  torch.zeros_like(x["image_clip"], device=device).unsqueeze(1),
                                  torch.ones((x["input_ids"].shape[0], MAX_LENGTH), device=device),
                                  torch.tensor([1, 0], device=device).repeat(x["attention_mask"].shape[0], 1))

        # append final strings to each answer bin
        indexes = nn.functional.softmax(out, dim=-1).argmax(dim=-1)
        indexes = indexes.unique_consecutive(dim=-1)

        ans_strs = [dataset.tokenizer.decode(index) for index in indexes]

        GT_list = []
        for image_name in x["image"]:
            GT_list.append(['[CLS] ' + caption.strip().lower() + ' [SEP]' for caption in
                            dataset.data.loc[dataset.data['image'] == image_name]["caption"]])

        acc_bleu += metric(ans_strs, GT_list)

summary.write(f"BLEU-4 score: {acc_bleu / len(val_loader)}")

torch.save(val_set, f"{MODEL_NAME}.valset")

if not summary == sys.stdout:
    summary.close()
