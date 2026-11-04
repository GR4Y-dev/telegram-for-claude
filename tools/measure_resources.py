"""Read-only Linux measurements. No messages, environment values or process arguments.

Run once idle, during a call, and after several calls. A point sample is not peak RAM.
Use --model-dir, --venv-dir and repeat --image to add disk/image byte totals.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time


def command(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=15).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def tree_bytes(folder):
    if folder is None or not folder.is_dir():
        return None
    try:
        # Logical file bytes, deliberately not the filesystem's allocated blocks.
        return sum(p.lstat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())
    except OSError:
        return None


def process_tree(pid):
    parents = {}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            fields = (proc / "stat").read_text().rpartition(")")[2].split()
            parents[int(proc.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    children = {pid}
    while True:
        expanded = children | {child for child,parent in parents.items() if parent in children}
        if expanded == children:
            return children
        children = expanded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default="telegram-caller")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--venv-dir", type=Path)
    parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", args.service):
        parser.error("Use a service name")
    def service_counter(field):
        value = command(["systemctl", "show", "-p", field, "--value", "--", args.service])
        return int(value) if value and value.isdecimal() else None

    pid = service_counter("MainPID")
    totals = {"rss_bytes":0,"swap_bytes":0,"processes":0}
    if pid and pid > 0:
        for child in process_tree(pid):
            try:
                status = (Path("/proc") / str(child) / "status").read_text()
                for field,key in (("VmRSS","rss_bytes"),("VmSwap","swap_bytes")):
                    match = re.search(rf"^{field}:\s+(\d+)\s+kB$", status, re.M)
                    if match:
                        totals[key] += int(match[1]) * 1024
                totals["processes"] += 1
            except OSError:
                pass
    else:
        totals = None
    images = []
    for image in args.image:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*",image):
            parser.error("Use an image name without options or credentials")
        size = command(["docker","image","inspect","--format={{.Size}}",image])
        images.append({"image":image,"bytes":int(size) if size and size.isdecimal() else None})
    print(json.dumps({"sample_unix_time":time.time(),"caller_process_tree":totals,
        "service_memory_current_bytes":service_counter("MemoryCurrent"),
        "service_memory_peak_bytes":service_counter("MemoryPeak"),
        "model_logical_bytes":tree_bytes(args.model_dir),"venv_logical_bytes":tree_bytes(args.venv_dir),
        "images":images,
        "note":"Guest containers are daemon-owned; use docker stats separately. Point samples are not peaks."},indent=2))


if __name__ == "__main__":
    main()
