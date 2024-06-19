# For docs, see ../setup.py
import argparse
import importlib
import json
import logging
import os
import os.path
import pdb
import shutil
import subprocess
import sys
import traceback

from itertools import islice
from requests_ratelimiter import LimiterSession

import markdown
import requests

# Create logger that logs to standard error
logger = logging.getLogger("anki-summarize-notes")
# These 2 lines prevent duplicate log lines.
logger.handlers.clear()
logger.propagate = False

LEVEL_DEFAULT = logging.INFO
level = os.environ.get("ANKI_SUMMARIZE_NOTES_LOGLEVEL")
if level:
    level = level.upper()
else:
    level = LEVEL_DEFAULT
logger.setLevel(level)

# Create handler that logs to standard error
handler = logging.StreamHandler()
handler.setLevel(level)

# Create formatter and add it to the handler
formatter = logging.Formatter("[%(levelname)8s %(asctime)s - %(name)s] %(message)s")
handler.setFormatter(formatter)

# Add handler to the logger
logger.addHandler(handler)

ANKICONNECT_URL_DEFAULT = "http://localhost:8765"
ankiconnect_url = os.environ.get(
    "ANKI_SUMMARIZE_NOTES_ANKICONNECT_URL", ANKICONNECT_URL_DEFAULT
)
ANKICONNECT_VERSION = 6


# Load secrets from pockexport for use by pocket module.
loader = importlib.machinery.SourceFileLoader(
    "secrets", os.path.expanduser("~/.config/anki-summarize-notes/secrets.py")
)
spec = importlib.util.spec_from_loader("secrets", loader)
secrets = importlib.util.module_from_spec(spec)
loader.exec_module(secrets)


def batched(iterable, n):
    "Batch data into tuples of length n. The last batch may be shorter."
    # batched('ABCDEFG', 3) --> ABC DEF G
    if n < 1:
        raise ValueError("n must be at least one")
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch


class SessionRequest:
    def __init__(self, session=None):
        if session:
            self.session = session
        else:
            self.session = requests.Session()


class AnkiConnectRequest(SessionRequest):
    def request(self, payload):
        payload["version"] = ANKICONNECT_VERSION
        logger.debug("payload = %s", payload)
        response = json.loads(
            self.session.post(ankiconnect_url, json=payload, timeout=3).text
        )
        logger.debug("response = %s", response)
        if response["error"] is not None:
            logger.warning("payload %s had response error: %s", payload, response)
        return response


class JinaAIContentRequest(SessionRequest):
    def __init__(self, session=None):
        if session:
            self.session = session
        else:
            self.session = LimiterSession(per_minute=100)
            self.session.headers.update(
                {"Authorization": f"Bearer {secrets.jina_api_key}"}
            )

    def request(self, url):
        logger.debug("url = %s", url)
        response = self.session.get(f"https://r.jina.ai/{url}", timeout=15).text
        logger.debug("response = %s", response)
        return response


ankiconnect_request = AnkiConnectRequest()
jina_ai_content_request = JinaAIContentRequest()

BATCH_SIZE = 25

MODEL_NAME = "gpt-4o"


def anki_sync():
    logger.info("Syncing Anki")
    return ankiconnect_request.request({"action": "sync"})


def markdown_llm_summarize(content):
    proc = subprocess.Popen(
        [shutil.which("summarize"), "-p", MODEL_NAME],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    (summary, _) = proc.communicate(input=content.encode("utf-8"), timeout=60)
    if proc.returncode != 0:
        raise Exception(f"summarize failed - returncode = {proc.returncode}")
    return summary.decode(encoding="utf-8", errors="strict")


def main():
    try:
        rc = _main()
        if rc is not None:
            sys.exit(rc)
    except Exception:
        debug = os.environ.get("ANKI_SUMMARIZE_NOTES_DEBUG", None)
        if debug and debug != "0":
            _extype, _value, tb = sys.exc_info()
            traceback.print_exc()
            pdb.post_mortem(tb)
        else:
            raise


def _main():
    parser = argparse.ArgumentParser(
        prog="anki-summarize-notes",
        description="Use LLM to summarize Anki articles",
        epilog=f"""Environment variables:

- ANKI_SUMMARIZE_NOTES_ANKICONNECT_URL: set to the URL of AnkiConnect. Default:
  {ANKICONNECT_URL_DEFAULT}
  set to "{ANKICONNECT_URL_DEFAULT}".
- ANKI_SUMMARIZE_NOTES_DEBUG: set in order to debug using PDB upon exception.
- ANKI_SUMMARIZE_NOTES_LOGLEVEL: set log level. Default: {LEVEL_DEFAULT}
""",
    )
    parser.add_argument(
        "--summarize-method",
        type=str,
        help="Method used to summarize notes.",
        default="anki-summarize-to-clipboard-gpt4o",
    )
    deck_name = "Articles"
    note_type = "Pocket Article"
    parser.add_argument(
        "--dry-run", help="Dry-run mode.", action="store_true", required=False
    )
    parser.add_argument(
        "--query",
        help="Anki query to find notes. --edited argument is added to this query if present.",
        type=str,
        default=f'("note:{note_type}" "deck:{deck_name}" summary:)',
    )
    parser.add_argument(
        "--limit-count", type=int, help="Only process the first N notes."
    )
    parser.add_argument(
        "--edited", type=int, help="Only examine notes modified in the past N days."
    )
    args = parser.parse_args()

    anki_sync()

    response = ankiconnect_request.request(
        {
            "action": "findNotes",
            "params": {
                "query": f'{args.query}{f" edited:{args.edited}" if args.edited else ""}',
            },
        }
    )
    note_ids = response["result"]
    if not note_ids:
        logger.info(
            'No matching notes found{f" and got error {response["error"]}" if response["error"] else ""} - check values of flags passed to this program'
        )
        return 0
    if args.limit_count:
        logger.info(
            f"Only keeping the first {args.limit_count} notes without an existing summary."
        )
    logger.info(f"note_ids: {note_ids}")

    response = ankiconnect_request.request(
        {
            "action": "notesInfo",
            "params": {
                "notes": note_ids,
            },
        }
    )
    note_infos = response["result"]
    processed_count = 0
    if note_infos:
        try:
            for batch in batched(note_infos, BATCH_SIZE):
                if args.limit_count and processed_count >= args.limit_count:
                    break
                actions = []
                for note_info in batch:
                    if args.limit_count and processed_count >= args.limit_count:
                        break
                    summary = note_info["fields"]["summary"]["value"]
                    if summary:
                        logger.info(
                            "Skipping note ID %d because summary already present",
                            note_info["noteId"],
                        )
                        continue
                    processed_count += 1
                    url = note_info["fields"]["resolved_url"]["value"]
                    url_content = jina_ai_content_request.request(url)
                    summary = markdown_llm_summarize(url_content)
                    summary_html = markdown.markdown(summary)
                    new_fields = {
                        "summary": summary_html,
                    }
                    update_action = {
                        "action": "updateNoteFields",
                        "params": {
                            "note": {
                                "id": note_info["noteId"],
                                "fields": new_fields,
                            },
                        },
                    }
                    if args.dry_run:
                        logger.info(
                            "Would update note %s with fields %s",
                            note_info["noteId"],
                            new_fields,
                        )
                    else:
                        logger.debug(
                            "Updating note %s with fields %s",
                            note_info["noteId"],
                            new_fields,
                        )
                        actions.append(update_action)

                response = ankiconnect_request.request(
                    {
                        "action": "multi",
                        "params": {"actions": actions},
                    }
                )
        finally:
            anki_sync()


if __name__ == "__main__":
    main()
