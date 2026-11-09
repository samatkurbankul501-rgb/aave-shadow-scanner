"""logger: JSONL + сводный JSON по проходу.

Из спеки: log_entry(candidate_dict), summary(pass_dict).
Формат JSONL: {ts, user, hf, debt_usd, collateral_usd, net_profit_usd, block, block_hash}.
Формат сводного: {pass_start_ts, pass_end_ts, candidates_checked, liquidatable,
net_positive, sum_net_profit_usd, candidates}.

Дедупликация — внутри одного прохода (_seen сбрасывается каждой новой
записью через reset_pass()); JSONL-файл один на проход
(candidates_<pass_start_ts>.jsonl), а не на каждую запись.
"""
import json
import time
from pathlib import Path

_seen = {}
_pass_file = None


def reset_pass():
    global _seen, _pass_file
    _seen = {}
    _pass_file = None


def log_entry(entry: dict, out_dir: Path = None) -> dict:
    """Добавляет запись; при дубле по user в том же проходе — обновляет hf/ts
    и в памяти, и в JSONL (в файле ровно одна строка на user, свежий HF)."""
    global _pass_file
    user = entry.get("user", "")
    if user in _seen:
        old = _seen[user]
        merged = {**old, "hf": entry.get("hf", old.get("hf")), "ts": entry.get("ts", old.get("ts"))}
        _seen[user] = merged
        if _pass_file is not None and _pass_file.exists():
            lines = _pass_file.read_text(encoding="utf-8").splitlines()
            out = []
            for line in lines:
                try:
                    rec = json.loads(line)
                except Exception:
                    out.append(line)
                    continue
                out.append(json.dumps(merged, ensure_ascii=False) if rec.get("user") == user else line)
            _pass_file.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        return merged
    _seen[user] = dict(entry)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        if _pass_file is None:
            _pass_file = out_dir / f"candidates_{int(time.time())}.jsonl"
        with open(_pass_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return _seen[user]


def summary(pass_dict: dict, out_dir: Path = None) -> Path:
    """Пишет сводный JSON по проходу, возвращает путь."""
    global _pass_file
    if out_dir is None:
        out_dir = Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    fpath = out_dir / f"pass_{int(time.time())}.json"
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(pass_dict, f, ensure_ascii=False, indent=2)
    _pass_file = None
    return fpath
