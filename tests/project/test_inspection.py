import asyncio

import pytest

from maajun.config import AIProviderConfig
from maajun.project.inspection import (
    INSPECT_ROUNDS,
    MAX_SURVEY_CHARS,
    Inspection,
    absolute,
    inspect_repo,
    make_agent,
    parse_json,
    strings,
    survey,
    whole_number,
)
from maajun.providers.base import CompletionResponse

ANSWER = """{
  "stack": "Django 5 + gunicorn",
  "entrypoint": "gunicorn shop.wsgi -b 0.0.0.0:8000",
  "port": 8000,
  "log_files": ["logs/django-error.log", "/var/log/nginx/error.log"],
  "log_format": "text",
  "json_level_field": "",
  "error_pattern": "",
  "logging_gaps": ["views.py:11 - except Exception: pass swallows DB errors"],
  "logging_advice": "Create the logs/ directory at startup",
  "risky_areas": ["views.py:5 - unguarded session access"],
  "confidence": "high"
}"""


class FakeAgent:
    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.closed = False

    async def chat(self, message):
        self.prompts.append(message)
        content = self.replies.pop(0) if self.replies else ""
        return CompletionResponse(content=content, usage={"prompt_tokens": 10})

    async def aclose(self):
        self.closed = True


@pytest.fixture
def ai():
    return AIProviderConfig(api_key="sk-test")


def inspect(path, ai, agent):
    return asyncio.run(inspect_repo(path, ai, agent=agent))


# ---------------------------------------------------------------------------
# Reading an answer
# ---------------------------------------------------------------------------


def test_the_findings_become_config(tmp_path, ai):
    agent = FakeAgent(ANSWER)

    found = inspect(tmp_path, ai, agent)

    assert found.stack == "Django 5 + gunicorn"
    assert found.port == 8000
    assert found.log_files == [
        str(tmp_path / "logs" / "django-error.log"),
        "/var/log/nginx/error.log",
    ]
    assert found.logging_advice.startswith("Create the logs")
    assert agent.closed


def test_a_repo_relative_path_becomes_watchable(tmp_path):
    """"logs/error.log" is not a path maajun can tail from anywhere."""
    assert absolute(["logs/error.log"], tmp_path) == [
        str(tmp_path / "logs" / "error.log")
    ]


def test_a_fenced_answer_is_still_read(tmp_path, ai):
    agent = FakeAgent(f"```json\n{ANSWER}\n```")

    assert inspect(tmp_path, ai, agent).stack == "Django 5 + gunicorn"


def test_an_answer_with_a_sentence_in_front_is_still_read(tmp_path, ai):
    agent = FakeAgent(f"Here is what I found:\n{ANSWER}")

    assert inspect(tmp_path, ai, agent).port == 8000


def test_a_long_answer_is_not_truncated_before_parsing(tmp_path, ai):
    """Regression: capping the text first cut the JSON in half and threw away
    a perfectly good reading of the code."""
    padded = ANSWER.replace(
        '"confidence": "high"',
        '"notes": "' + "x" * 30_000 + '", "confidence": "high"',
    )
    agent = FakeAgent(padded)

    assert inspect(tmp_path, ai, agent).confidence == "high"


def test_prose_gets_one_more_chance(tmp_path, ai):
    agent = FakeAgent("I had a look and the app seems fine.", ANSWER)

    found = inspect(tmp_path, ai, agent)

    assert found.stack == "Django 5 + gunicorn"
    assert len(agent.prompts) == 2
    assert "only the JSON" in agent.prompts[1]


def test_an_answer_that_never_parses_yields_nothing(tmp_path, ai):
    agent = FakeAgent("no json", "still no json")

    found = inspect(tmp_path, ai, agent)

    assert not found.has_findings()
    assert found.log_files == []


def test_a_missing_directory_is_refused(ai):
    with pytest.raises(ValueError, match="not a directory"):
        inspect("/nonexistent/place", ai, FakeAgent(ANSWER))


# ---------------------------------------------------------------------------
# Field coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (8000, 8000),
    ("8000", 8000),
    ("not a port", 0),
    (0, 0),
    (99999, 0),
    (None, 0),
])
def test_a_port_is_only_kept_when_it_is_one(value, expected):
    assert whole_number(value) == expected


def test_a_single_string_is_read_as_a_list():
    """Models answer "risky_areas": "views.py" as often as a list."""
    assert strings("views.py:5") == ["views.py:5"]
    assert strings(["a", "", "  ", "b"]) == ["a", "b"]
    assert strings(None) == []
    assert strings(42) == []


def test_a_runaway_string_is_cut_to_a_readable_length():
    assert len(strings(["x" * 5000])[0]) == 300


@pytest.mark.parametrize("text", ["", "no braces here", "{ not json", "[1, 2]"])
def test_what_is_not_a_json_object(text):
    assert parse_json(text) == {}


def test_findings_are_recognised():
    assert Inspection(stack="Django").has_findings()
    assert Inspection(log_files=["/x.log"]).has_findings()
    assert not Inspection(confidence="low").has_findings()


# ---------------------------------------------------------------------------
# Reading the repo before asking
# ---------------------------------------------------------------------------


def make_app(root):
    (root / "shop").mkdir()
    (root / "requirements.txt").write_text("Django==5.0.3\n")
    (root / "Dockerfile").write_text("CMD gunicorn shop.wsgi -b 0.0.0.0:8000\n")
    (root / "shop" / "settings.py").write_text(
        'LOGGING = {"handlers": {"f": {"class": "logging.FileHandler"}}}\n'
    )
    (root / "shop" / "views.py").write_text("def checkout(request):\n    pass\n")
    return root


def test_the_survey_finds_the_files_that_answer_the_question(tmp_path):
    """Every round spent looking for settings.py is a round not answering."""
    material = survey(make_app(tmp_path))

    assert "requirements.txt" in material
    assert "Dockerfile" in material
    assert "settings.py" in material
    assert "logging.FileHandler" in material


def test_a_file_is_included_for_what_is_in_it(tmp_path):
    """A logging config does not have to be called settings.py."""
    (tmp_path / "boot.py").write_text("import logging\nlogging.basicConfig()\n")

    assert "boot.py" in survey(tmp_path)


def test_noise_directories_are_skipped(tmp_path):
    make_app(tmp_path)
    junk = tmp_path / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("createLogger()\n")

    material = survey(tmp_path)

    assert "node_modules" not in material


def test_the_survey_will_not_send_a_secret(tmp_path):
    """It goes straight to the provider, so it obeys the same boundary the
    agent's own read_file does — a .env must not arrive by the back door."""
    make_app(tmp_path)
    (tmp_path / ".env").write_text("DB_PASSWORD=hunter2\n")
    (tmp_path / ".env.example").write_text("DB_PASSWORD=hunter2\n")
    (tmp_path / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n")

    material = survey(tmp_path)

    assert "hunter2" not in material
    assert "PRIVATE KEY" not in material
    assert "settings.py" in material  # still finds what it is for


def test_the_survey_is_bounded(tmp_path):
    """It goes into every request, so a huge repo must not blow it up."""
    (tmp_path / "settings.py").write_text("FileHandler\n" + "x" * 200_000)
    for n in range(50):
        (tmp_path / f"mod{n}.py").write_text("basicConfig\n" + "y" * 50_000)

    assert len(survey(tmp_path)) <= MAX_SURVEY_CHARS + 200


def test_the_material_is_handed_over_with_the_question(tmp_path, ai):
    make_app(tmp_path)
    agent = FakeAgent(ANSWER)

    inspect(tmp_path, ai, agent)

    assert "logging.FileHandler" in agent.prompts[0]
    assert "only use read_file/grep/glob if something essential is" in agent.prompts[0]


def test_the_inspection_agent_cannot_browse_forever(tmp_path):
    """A quick job: a low round cap, not the daemon's fifty."""
    from maajun.agent.core import MAX_TOOL_ROUNDS

    agent = make_agent(AIProviderConfig(api_key="sk-test"), tmp_path)

    assert agent.max_rounds == INSPECT_ROUNDS
    assert INSPECT_ROUNDS < MAX_TOOL_ROUNDS
