"""Resource benchmarks: peak memory and duration per operation, in-container.

Generates large fixtures (never committed), runs each scenario through the
real container image, and reports two memory numbers per run:

- process peak RSS (ru_maxrss of the pdf_ops process) - what the
  application itself needs;
- cgroup memory.peak - what Kubernetes meters against limits, which also
  counts the (reclaimable) page cache the run touched.

Usage: python3 scripts/benchmark.py [--image pdf-ops:bench] [--workdir DIR]
The workdir defaults to /tmp/pdfops-bench (a path Docker Desktop shares).
"""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pikepdf

MB = 1024 * 1024

# One page of PDF structure per megabyte of incompressible payload: random
# bytes in uncompressed content streams, so file sizes are honest and the
# engine cannot shrink them away.
PAGE_STREAM_BYTES = 1 * MB

# Runs the operation under a tiny wrapper so ru_maxrss of the child (the
# real entrypoint) and the cgroup peak are both reported on stderr.
WRAPPER = (
    "import resource, subprocess, sys\n"
    "proc = subprocess.run([sys.executable, '-m', 'pdf_ops'])\n"
    "rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss\n"
    "print(f'PEAK_RSS_KB={rss_kb}', file=sys.stderr)\n"
    "try:\n"
    "    peak = open('/sys/fs/cgroup/memory.peak').read().strip()\n"
    "    print(f'CGROUP_PEAK={peak}', file=sys.stderr)\n"
    "except OSError:\n"
    "    pass\n"
    "sys.exit(proc.returncode)\n"
)


def make_pdf(path: Path, size_mb: int) -> None:
    if path.exists():
        return
    pdf = pikepdf.Pdf.new()
    for _ in range(size_mb):
        page = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Page,
                MediaBox=[0, 0, 612, 792],
                Contents=pdf.make_stream(os.urandom(PAGE_STREAM_BYTES)),
            )
        )
        pdf.pages.append(pikepdf.Page(page))
    pdf.save(path)


def make_encrypted_pdf(path: Path, size_mb: int, password: str) -> None:
    if path.exists():
        return
    plain = path.with_suffix(".plain.tmp")
    make_pdf(plain, size_mb)
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user=password, owner=password, R=6))
    plain.unlink()


def make_attachment_carrier(path: Path, count: int, each_mb: int) -> None:
    if path.exists():
        return
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    for index in range(count):
        name = f"payload-{index:02d}.bin"
        spec = pikepdf.AttachedFileSpec(
            pdf,
            os.urandom(each_mb * MB),
            description="benchmark payload",
            filename=name,
            mime_type="application/octet-stream",
            creation_date="",
            mod_date="",
        )
        pdf.attachments[name] = spec
    pdf.save(path)


def run_container(image: str, env: dict[str, str], volumes: dict[Path, str]) -> dict[str, object]:
    cmd = ["docker", "run", "--rm", "--entrypoint", "python"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    for host, spec in volumes.items():
        cmd += ["-v", f"{host}:{spec}"]
    cmd += [image, "-c", WRAPPER]

    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    wall = time.monotonic() - started

    events = [json.loads(line) for line in proc.stdout.strip().splitlines() if line]
    terminal = events[-1] if events else {}
    rss_kb = cgroup_peak = None
    for line in proc.stderr.splitlines():
        if line.startswith("PEAK_RSS_KB="):
            rss_kb = int(line.split("=", 1)[1])
        elif line.startswith("CGROUP_PEAK="):
            cgroup_peak = int(line.split("=", 1)[1])
    return {
        "exit": proc.returncode,
        "wall_s": round(wall, 1),
        "duration_s": terminal.get("duration_s"),
        "rss_mb": round(rss_kb / 1024) if rss_kb else None,
        "cgroup_mb": round(cgroup_peak / MB) if cgroup_peak else None,
        "event": terminal.get("event"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="pdf-ops:bench")
    parser.add_argument("--workdir", default="/tmp/pdfops-bench")
    args = parser.parse_args()

    base = Path(args.workdir)
    fixtures = base / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)

    print("generating fixtures (cached across runs) ...")
    make_pdf(fixtures / "small-a.pdf", 5)
    make_pdf(fixtures / "small-b.pdf", 5)
    make_pdf(fixtures / "big-a.pdf", 250)
    make_pdf(fixtures / "big-b.pdf", 250)
    for index in range(20):
        make_pdf(fixtures / f"mid-{index:02d}.pdf", 25)
    make_encrypted_pdf(fixtures / "big-locked.pdf", 250, "bench-password")
    make_attachment_carrier(fixtures / "carrier.pdf", 10, 25)
    (fixtures / "pw").write_text("bench-password\n")

    scenarios: list[tuple[str, dict[str, str]]] = [
        (
            "merge 2 x 5 MB (baseline)",
            {
                "PDFOPS_OPERATION": "merge",
                "PDFOPS_INPUTS": "/in/small-a.pdf:/in/small-b.pdf",
                "PDFOPS_OUTPUT": "/out/baseline.pdf",
            },
        ),
        (
            "merge 2 x 250 MB",
            {
                "PDFOPS_OPERATION": "merge",
                "PDFOPS_INPUTS": "/in/big-a.pdf:/in/big-b.pdf",
                "PDFOPS_OUTPUT": "/out/big.pdf",
            },
        ),
        (
            "merge 20 x 25 MB",
            {
                "PDFOPS_OPERATION": "merge",
                "PDFOPS_INPUTS": ":".join(f"/in/mid-{i:02d}.pdf" for i in range(20)),
                "PDFOPS_OUTPUT": "/out/many.pdf",
            },
        ),
        (
            "merge 250 MB AES-256 in, re-encrypted out",
            {
                "PDFOPS_OPERATION": "merge",
                "PDFOPS_INPUTS": "/in/big-locked.pdf",
                "PDFOPS_OUTPUT": "/out/relocked.pdf",
                "PDFOPS_PASSWORD_FILE": "/in/pw",
                "PDFOPS_OUTPUT_ENCRYPTION": "inherit",
            },
        ),
        (
            "extract 10 x 25 MB attachments",
            {
                "PDFOPS_OPERATION": "extract",
                "PDFOPS_INPUT": "/in/carrier.pdf",
                "PDFOPS_OUTPUT_DIR": "/out",
            },
        ),
    ]

    print(f"{'scenario':44} {'exit':>4} {'op_s':>7} {'rss_mb':>7} {'cgroup_mb':>9}")
    results: list[tuple[str, dict[str, object]]] = []
    for name, env in scenarios:
        out_dir = base / "out"
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir()
        out_dir.chmod(0o777)
        result = run_container(args.image, env, {fixtures: "/in:ro", out_dir: "/out"})
        results.append((name, result))
        print(
            f"{name:44} {result['exit']:>4} {result['duration_s']!s:>7} "
            f"{result['rss_mb']!s:>7} {result['cgroup_mb']!s:>9}"
        )

    print("\nmarkdown:")
    print("| Scenario | Duration | Peak process RSS | Peak cgroup memory |")
    print("|---|---|---|---|")
    for name, result in results:
        print(
            f"| {name} | {result['duration_s']} s | ~{result['rss_mb']} MB "
            f"| ~{result['cgroup_mb']} MB |"
        )


if __name__ == "__main__":
    main()
