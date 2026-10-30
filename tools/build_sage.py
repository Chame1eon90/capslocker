# -*- coding: utf-8 -*-
"""Конвертация модели SAGE (Сбер) для офлайн-проверки ошибок в формат CTranslate2 (int8).
Запускается на GitHub (workflow models.yml). Проверяет, что сжатая модель исправляет так же,
как исходная, и сохраняет настройки (нужен ли префикс <LM>) в sage_config.json."""
import json, os, shutil, subprocess, sys, zipfile

REPO = "ai-forever/sage-fredt5-distilled-95m"
OUT = "sage_ru"
TESTS = ["превет как дила", "я пашол в магазин и купил хлеп", "привет как дела что делаеш",
         "нужен танк в подземелье кто пойдет"]

from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import torch

tok = AutoTokenizer.from_pretrained(REPO)
model = AutoModelForSeq2SeqLM.from_pretrained(REPO).eval()


def hf(text, prefix):
    enc = tok(prefix + text, return_tensors="pt")
    with torch.inference_mode():
        out = model.generate(**enc, max_length=int(enc["input_ids"].size(1) * 1.5) + 8, num_beams=1)
    return tok.batch_decode(out, skip_special_tokens=True)[0].strip()


def score(prefix):
    outs = [hf(t, prefix) for t in TESTS]
    good = sum(1 for o in outs if "привет" in o.lower() or "пошёл" in o.lower() or "пошел" in o.lower()
               or "дела" in o.lower())
    return good, outs


best = None
for prefix in ("", "<LM>"):
    g, outs = score(prefix)
    print(f"prefix={prefix!r} score={g}", outs, flush=True)
    if best is None or g > best[0]:
        best = (g, prefix, outs)
prefix = best[1]

if os.path.isdir(OUT):
    shutil.rmtree(OUT)
subprocess.run(["ct2-transformers-converter", "--model", REPO, "--output_dir", OUT,
                "--quantization", "int8", "--force"], check=True)
tok.save_pretrained(OUT + "_tok")
tj = os.path.join(OUT + "_tok", "tokenizer.json")
if not os.path.exists(tj):
    tok.backend_tokenizer.save(tj)
shutil.copy(tj, os.path.join(OUT, "tokenizer.json"))

# проверяем ровно тем способом, каким будет пользоваться программа
import ctranslate2, tokenizers
tr = ctranslate2.Translator(OUT, device="cpu", compute_type="int8")
t2 = tokenizers.Tokenizer.from_file(os.path.join(OUT, "tokenizer.json"))
ct_outs = []
for t in TESTS:
    toks = t2.encode(prefix + t).tokens
    res = tr.translate_batch([toks], beam_size=1, max_decoding_length=int(len(toks) * 1.5) + 8)
    ids = [t2.token_to_id(x) for x in res[0].hypotheses[0]]
    ct_outs.append(t2.decode([i for i in ids if i is not None], skip_special_tokens=True).strip())
print("ct2:", ct_outs, flush=True)
same = sum(1 for a, b in zip(best[2], ct_outs) if a == b)
cfg = {"prefix": prefix, "model": REPO, "hf_outputs": best[2], "ct2_outputs": ct_outs, "same": same}
json.dump(cfg, open(os.path.join(OUT, "sage_config.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a", encoding="utf-8") as f:
    f.write(f"prefix={prefix!r}, совпадений {same}/{len(TESTS)}\n\nHF: {best[2]}\n\nCT2: {ct_outs}\n")
print(f"::notice title=SAGE::prefix={prefix!r} same={same}/{len(TESTS)} | HF={best[2]} | CT2={ct_outs}")
if same < len(TESTS) // 2:
    sys.exit("Сжатая модель исправляет не так, как исходная")
with zipfile.ZipFile("sage-ru-ct2-int8.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for fn in os.listdir(OUT):
        z.write(os.path.join(OUT, fn), "sage_ru/" + fn)
print("zip", os.path.getsize("sage-ru-ct2-int8.zip"))
