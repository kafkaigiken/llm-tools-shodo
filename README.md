# llm-tools-shodo

[![PyPI](https://img.shields.io/pypi/v/llm-tools-shodo.svg)](https://pypi.org/project/llm-tools-shodo/)
[![Changelog](https://img.shields.io/github/v/release/kafkaigiken/llm-tools-shodo?include_prereleases&label=changelog)](https://github.com/kafkaigiken/llm-tools-shodo/releases)
[![Tests](https://github.com/kafkaigiken/llm-tools-shodo/actions/workflows/test.yml/badge.svg)](https://github.com/kafkaigiken/llm-tools-shodo/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/kafkaigiken/llm-tools-shodo/blob/main/LICENSE)

An [llm](https://llm.datasette.io/) plugin that proofreads Japanese text with the
[Shodo](https://shodo.ink/) AI proofreading API. We use this plugin to proofread
Japanese text in our [Kafkai Blog](https://kafkai.com/ja/blog/), [Kafkai AI Media](https://kafkai.ai/) and [PuchiDen Blog](https://puchiden.app/ja/blog/)
as part of our editorial pipeline.

The plugin is a simple Python script that uses the `llm` API to add a new command and tool.

## Features

It adds two things:

- `llm shodo`: a command that reads text and prints the corrected text. No model is
  involved, so only Shodo's fixes change the text. Every other byte, including CRLF
  line endings, stays as it was.
- `shodo_proofread`: an llm tool, for when you want a model to proofread and then
  discuss or rework the text.

Both apply every Shodo suggestion that has a replacement and a score of at least 0.5.
The official `shodo lint` command hides suggestions below 0.5, so the plugin lists them
as issues instead of applying them. Both also list the issues that have no automatic fix.
Empty input never reaches the API.

## Installation

Install this plugin in the same environment as [LLM](https://llm.datasette.io/).
You need LLM 0.33 or newer and Python 3.10 or newer.

```bash
llm install llm-tools-shodo
```

To install the latest code from GitHub before a release reaches PyPI:

```bash
llm install https://github.com/kafkaigiken/llm-tools-shodo/archive/refs/heads/main.zip
```

## Credentials

1. In Shodo, open the project, then **API連携** (API settings), and create a token.
2. Store the token and set the API root:

```bash
llm keys set shodo        # paste the token
export SHODO_API_ROOT="https://api.shodo.ink/@your-org/your-project/"
```

The root and the token always come from the same source, so a token is never sent to
another project's root. The plugin uses the first complete pair it finds:

1. `SHODO_API_ROOT` and `SHODO_API_TOKEN`, both set. These override a stored key.
2. `SHODO_API_ROOT` and the token stored with `llm keys set shodo`.
3. The `default` profile in `~/.config/shodo/credentials`, the file that the official
   `shodo login` command writes. The plugin reads `~/.shodo/credentials` when that file
   does not exist.

## Usage

To fix a file, run `llm shodo`. The input must be UTF-8. The corrected text goes to
stdout, and the fix report goes to stderr:

```bash
cat article.md | llm shodo > fixed.md
llm shodo article.md -q          # -q: no report
llm shodo page.html --html       # HTML input
```

To proofread inside a model conversation, use the tool:

```bash
cat article.md | llm -T shodo_proofread "Proofread this, then explain the main issues." --td
```

(Omit `--td` if you don't want to see the tool call details.)

This repository also ships a template, [`templates/shodo.yaml`](templates/shodo.yaml).
It tells the model to call the tool and return the corrected text unchanged. To install
it as the `shodo` template:

```bash
cp templates/shodo.yaml "$(llm templates path)/shodo.yaml"
cat article.md | llm -t shodo
```

## Development

To set up this plugin locally, first check out the code. Then create a new virtual
environment:

```bash
cd llm-tools-shodo
python -m venv venv
source venv/bin/activate
```

Now install the dependencies and test dependencies:

```bash
llm install -e '.[test]'
```

To run the tests:

```bash
python -m pytest
```

The tests use `httpx2.MockTransport` in place of the real API, so they need no Shodo token.
