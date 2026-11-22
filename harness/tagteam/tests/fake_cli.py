#!/usr/bin/env python3
"""Stand-in for the claude, codex and agy CLIs. Usage: fake_cli.py <claude|openai|google> [CLI args...]

Records argv, stdin, HARNESS_DELEGATE_RUN, CLAUDECODE, CLAUDE_CODE_ENTRYPOINT, ANTIGRAVITY_CONVERSATION_ID, pid,
the text of the file named by FAKE_PEEK at start (the launcher's stdout, when a test redirects it there) and the text
of started.json in the run folder (found from -o or --log-file; None if absent) to FAKE_RECORD (JSON),
then behaves per FAKE_MODE:
ok, wrong_model, no_model_field, exhausted, exhausted_epoch, quota_text_in_prompt, crash, sleep, empty (google),
write_file (writes fake_written.txt in the cwd, then ok), commit (makes an empty git commit in the cwd, then ok).
Options, whatever the mode: FAKE_WRITE (JSON object path -> text, null deletes; relative paths are in the cwd) is
applied first (FAKE_ANSWER: the answer text, default OK), then FAKE_LINK (JSON object path -> target: the file or folder is replaced by a symbolic link, on Windows a folder
possibly by a junction); FAKE_STDERR is written to stderr; openai writes a role record like a Codex hook would, to
$TAGTEAM_STATE_DIR/sessions, with host FAKE_SESSION (default codex; "none" writes nothing).
"""
import json
import os
import shutil
import subprocess
import sys
import time

QUOTA_TEXT = "the log says 429 usage limit reached"
EPOCH = 1900000000


def answer():
    return os.environ.get("FAKE_ANSWER", "OK")


def opt(args, flag, default=""):
    return args[args.index(flag) + 1] if flag in args else default


def read_if_file(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return None


def replace_by_link(path, target):
    """Replace a file or folder by a symbolic link to target; on Windows a folder may become a junction instead."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)
    try:
        os.symlink(target, path, target_is_directory=os.path.isdir(target))
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt" and os.path.isdir(target) and subprocess.run(
            ["cmd", "/c", "mklink", "/J", path, target], capture_output=True).returncode == 0:
        return
    raise OSError("cannot make a link here")


def apply_options(vendor):
    for path, text in json.loads(os.environ.get("FAKE_WRITE") or "{}").items():
        if text is None:
            if os.path.exists(path):
                os.remove(path)
            continue
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    for path, target in json.loads(os.environ.get("FAKE_LINK") or "{}").items():
        replace_by_link(path, target)
    sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
    host = os.environ.get("FAKE_SESSION", "codex")
    if vendor == "openai" and host != "none":
        folder = os.path.join(os.environ["TAGTEAM_STATE_DIR"], "sessions")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "fake-%d.json" % os.getpid()), "w", encoding="utf-8") as fh:
            json.dump({"role": "worker", "host": host, "ts": int(time.time())}, fh)


def emit(code, out="", err=""):
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.stdout.flush()
    sys.exit(code)


def claude(args, mode, model):
    body = {"type": "result", "is_error": False, "result": answer(), "modelUsage": {model: {"inputTokens": 1}}}
    if mode == "wrong_model":
        body["modelUsage"] = {"claude-other-1": {}}
    elif mode == "no_model_field":
        del body["modelUsage"]
    elif mode in ("exhausted", "exhausted_epoch"):
        body.update(is_error=True, result="Claude AI usage limit reached|%d" % EPOCH if mode == "exhausted_epoch"
                    else "You've hit your usage limit.")
        del body["modelUsage"]
        emit(1, json.dumps(body))
    elif mode == "quota_text_in_prompt":
        body["result"] = QUOTA_TEXT
    emit(0, json.dumps(body))


def openai(args, mode, model):
    last = opt(args, "-o")
    header = {"wrong_model": "model: gpt-other-1\n", "no_model_field": ""}.get(mode, "model: %s\n" % model)
    if mode in ("exhausted", "exhausted_epoch"):
        emit(1, "", "ERROR: You've hit your usage limit.\n")
    if mode == "exhausted_codex":  # the real codex wording: curly apostrophe, a local time to try again
        emit(1, "", "ERROR: You\u2019ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
                    "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at %s.\n"
             % os.environ.get("FAKE_TRY_AGAIN", "4:14 AM"))
    text = QUOTA_TEXT if mode == "quota_text_in_prompt" else answer()
    if last:
        with open(last, "w", encoding="utf-8") as fh:
            fh.write(text)
    emit(0, "429 Too Many Requests\n" + text + "\n" if mode == "quota_text_in_prompt" else text + "\n", header)


def google(args, mode, model_arg):
    effort = opt(args, "--effort")
    base = model_arg[: -len(effort) - 1] if model_arg.endswith("-" + effort) else model_arg
    label = " ".join(w.capitalize() for w in base.split("-"))
    label = os.environ.get("FAKE_AGY_LABEL") or label  # what agy shows, when the id alone does not give it
    if mode == "wrong_model":
        label = "Other Model"
    log = ["INFO model 'x' not in local config, defaulting"]
    if mode != "no_model_field":
        log.append('INFO Propagating selected model override to backend: label="%s (%s)"'
                   % (label, os.environ.get("FAKE_AGY_EFFORT") or effort.capitalize()))
    with open(opt(args, "--log-file"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(log) + "\n")
    if mode in ("exhausted", "exhausted_epoch"):
        emit(1, json.dumps({"error": "RESOURCE_EXHAUSTED: quota exceeded"}))
    if mode == "empty":
        emit(0, json.dumps({"status": "SUCCESS", "response": "",
                            "denied_actions": [{"action": "command", "display_name": "Bash"}]}))
    emit(0, json.dumps({"response": QUOTA_TEXT if mode == "quota_text_in_prompt" else answer()}))


def main():
    vendor, args = sys.argv[1], sys.argv[2:]
    mode = os.environ.get("FAKE_MODE", "ok")
    stdin = "" if vendor == "google" else sys.stdin.buffer.read().decode("utf-8", errors="replace")
    record = os.environ.get("FAKE_RECORD")
    if record:
        peek = os.environ.get("FAKE_PEEK")
        if peek:
            with open(peek, encoding="utf-8", errors="replace") as fh:
                peek = fh.read()
        in_run_dir = opt(args, "-o") or opt(args, "--log-file")  # claude gets neither
        started = os.path.join(os.path.dirname(in_run_dir), "started.json") if in_run_dir else ""
        with open(record, "w", encoding="utf-8") as fh:
            json.dump({"argv": args, "stdin": stdin, "delegate_env": os.environ.get("HARNESS_DELEGATE_RUN"),
                       "claudecode": os.environ.get("CLAUDECODE"),
                       "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT"),
                       "antigravity": os.environ.get("ANTIGRAVITY_CONVERSATION_ID"),
                       "pid": os.getpid(), "peek": peek,
                       "started": read_if_file(started)}, fh)
    apply_options(vendor)
    if mode == "crash":
        echo = "model: %s\nuser\n%s\n" % (opt(args, "-m"), stdin) if vendor == "openai" else ""
        emit(1, "", echo + "boom\n")
    if mode == "sleep":
        time.sleep(60)
        emit(0)
    if mode == "write_file":  # then behaves like ok
        with open("fake_written.txt", "w", encoding="utf-8") as fh:
            fh.write("x")
    if mode == "commit":  # a worker that commits (needs a git work tree; failure is ignored)
        subprocess.run(["git", "-c", "user.name=fake", "-c", "user.email=fake@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "--allow-empty", "-m", "fake"], capture_output=True)
    model = opt(args, "--model") or opt(args, "-m")
    {"claude": claude, "openai": openai, "google": google}[vendor](args, mode, model)


if __name__ == "__main__":
    main()
