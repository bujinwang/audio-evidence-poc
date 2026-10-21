"""Application configuration.

Everything is environment-driven so the same image runs in a laptop demo and in
a locked-down deployment. Component selection (transcriber, denoiser, embedder,
vector store) is deliberately *not* hard-coded: each has an `auto` mode that
probes what is actually installed and degrades with a recorded reason instead of
crashing the pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Settings:
    env: str
    data_dir: Path
    database_url: str
    max_upload_mb: int

    # component selection
    transcriber: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: str
    diarization: bool
    hf_token: str
    denoiser: str
    embedder: str
    embedding_model: str
    vector_store: str
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: str

    # processing
    process_inline: bool
    worker_poll_seconds: float
    search_default_limit: int
    # Cosine floor for the *semantic* channel. The measurement table behind the
    # number, the derivation, and why it is scaled for small corpora are
    # documented where the value is built (`semantic_min_score=` in `_build`),
    # so there is exactly one authoritative copy to keep current.
    semantic_min_score: float
    # Floor for the lexical fallback embedder (`hashing-lexical-v2`), which sits
    # on a different cosine scale. See `search._semantic_cutoff`.
    lexical_min_score: float
    # Corpus size at which `semantic_min_score` applies unmodified. Smaller
    # corpora get it scaled down: max-cosine is an extreme order statistic and
    # grows with the number of candidates.
    semantic_floor_reference_pool: int
    search_exhibit_cap_share: float
    # demo / dev conveniences
    dev_default_actor: str
    dev_default_role: str
    allow_anonymous: bool
    cors_origins: tuple[str, ...]

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def working_dir(self) -> Path:
        return self.data_dir / "working"

    @property
    def denoised_dir(self) -> Path:
        return self.data_dir / "denoised"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def _build() -> Settings:
    data_dir = _path("DATA_DIR", REPO_ROOT / "data")
    database_url = os.getenv("DATABASE_URL") or f"sqlite:///{data_dir / 'app.db'}"
    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:8000")
    return Settings(
        env=os.getenv("APP_ENV", "dev"),
        data_dir=data_dir,
        database_url=database_url,
        max_upload_mb=_int("MAX_UPLOAD_MB", 512),
        transcriber=os.getenv("TRANSCRIBER", "auto").lower(),
        # `small` measured 3.9% WER on the noisy test set vs 4.7% for `base`,
        # at ~2.5x the compute — still 6x faster than realtime on CPU. Accuracy
        # matters more than throughput for evidence, so it is the default.
        # Use `base` for quick smoke tests.
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        whisper_device=os.getenv("WHISPER_DEVICE", "auto").lower(),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        # Language autodetection fails on short, quiet, or accented windows and
        # when it fails it fails *silently and catastrophically*: on AMI
        # distant-microphone audio it picked Welsh at p=0.73 and emitted
        # gibberish, scoring 77.0% WER where forcing English gave 40.4%.
        # An evidence transcript must not be in the wrong language, so `en` is
        # the default and `auto` is opt-in.
        whisper_language=os.getenv("WHISPER_LANGUAGE", "en").lower(),
        diarization=_bool("DIARIZATION", False),
        hf_token=os.getenv("HF_TOKEN", ""),
        denoiser=os.getenv("DENOISER", "auto").lower(),
        embedder=os.getenv("EMBEDDER", "auto").lower(),
        embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        vector_store=os.getenv("VECTOR_STORE", "auto").lower(),
        qdrant_url=os.getenv("QDRANT_URL", ""),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", "evidence_segments"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
        process_inline=_bool("PROCESS_INLINE", True),
        worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1.0")),
        search_default_limit=_int("SEARCH_DEFAULT_LIMIT", 20),
        # Absolute cosine floor below which a semantic hit is not a hit.
        #
        # Cosine similarity is never zero: every embedding sits in a narrow cone,
        # so even a nonsense query scores 0.27-0.35 against unrelated English
        # prose. A floor below that band cannot reject anything, and hybrid mode
        # then returns a full page for `asdfghjkl`, with RRF handing each
        # survivor a positive score — the UI presents noise as ranked evidence.
        # In a disclosure tool an honest empty screen is worth more than a
        # plausible-looking wrong one.
        #
        # Measured on the live 6-exhibit corpus (2315 segments), top-1 cosine:
        #
        #   nonsense   0.2676 qqqzzz blurfl   0.2923 xyzzy plugh
        #              0.2990 qwertyuiop       0.3035 zzzqqq
        #              0.3538 flurble wumpus   0.3540 asdfghjkl
        #   genuine    0.4532 commencement address
        #              0.4600 woman hiding from an attacker
        #              0.5102 a football game on the radio
        #              0.5442 who was in the meeting
        #              0.6400 police funding
        #              0.6874 a mission to the moon
        #
        # 0.38 sits in the gap between those bands and was verified to reject all
        # six nonsense queries while keeping all nine genuine ones non-empty.
        #
        # 0.40 is the default because it is the midpoint of the measured gap —
        # 0.046 above the noise ceiling and 0.053 below the genuine floor — so it
        # has margin on both sides instead of grazing the highest noise sample.
        # (0.38 leaves only 0.026 of margin and was rejected for that reason, not
        # because it failed to separate these 12 queries.)
        #
        # The *absolute value alone* is not portable across corpora, which is why
        # it is not the whole story: `small` scores 0.2882 against a three-line
        # synthetic transcript and 0.5002 against the real corpus, because short
        # passages carry less context and their cosine is compressed toward zero.
        # A plain 0.40 therefore rejected a legitimate hit on a small corpus —
        # it broke
        # `test_semantic_real.py::test_real_semantic_finds_concept_without_a_literal_word`
        # (the `small`/`smaller` regression) and starved
        # `test_exhibit_cap.py::test_single_exhibit_query_is_not_starved_in_each_mode`.
        #
        # The cause is that `max(cosine)` over a corpus is an extreme order
        # statistic: it rises with corpus size because a bigger corpus has more
        # chances to contain a spurious near-miss. Measured over random samples of
        # the demo corpus, the mean max-cosine of *nonsense* queries alone is
        # 0.158 at N=3, 0.195 at N=10, 0.241 at N=50, 0.296 at N=500. Comparing a
        # 3-candidate score against a 1000-candidate threshold is comparing two
        # different statistics.
        #
        # `search._semantic_cutoff` therefore scales this value down for corpora
        # smaller than `semantic_floor_reference_pool`, which is what makes a
        # single configured number usable at both ends. Both tests above pass with
        # 0.40 under that rule. Re-run them before changing either setting.
        #
        # Two short single-word queries fall below 0.40 and now return nothing:
        # `gun` (0.3771) and `woman` (0.3831). Checked rather than assumed, and
        # the loss is smaller than it looks:
        #   - neither word appears anywhere in the corpus (`gun`, `guns`,
        #     `weapon`, `firearm`, `pistol`, `rifle`, `shot`, `woman`, `women`
        #     all match 0 segments), so there is no passage that answers them and
        #     0 rows is the honest answer;
        #   - at 0.36 the rows they returned were `work with`, `to record`,
        #     `employees` — noise, of exactly the kind this floor exists to hide.
        # The cost of using 0.36 to keep them would be 0.006 of margin above
        # `asdfghjkl` (0.3540), i.e. a threshold grazing the highest noise sample,
        # which is the failure mode being fixed here.
        #
        # `helicopter` (0.3584) is also 0 rows, and that is correct rather than a
        # loss. It looks like a loss because the row it used to reach (`In
        # addition to flying two NASA science payloads,`) reads like an answer,
        # but it is a spurious max: only 0.0044 above the worst nonsense query,
        # and the passage is about a Blue Origin lunar mission — a helicopter is
        # not related to it. Measured, cos(helicopter, "flying") = 0.6365 while
        # cos(helicopter, that passage) = 0.3584. The concept still resolves for
        # every near-synonym (aircraft 0.4936, spacecraft 0.5632, aeroplane
        # 0.4338, rocket 0.4214 — each returns the same passage), so retrieval is
        # not broken; the word is simply absent from the corpus and unrelated to
        # what is in it. Keyword cannot rescue it for the same reason. A reranker
        # is what would surface the adjacent passage *as* loosely related; a lower
        # constant would only re-open the `asdfghjkl` leak.
        # See docs/redesign/README.md §14.
        semantic_min_score=float(os.getenv("SEMANTIC_MIN_SCORE", "0.40")),
        # Floor for the lexical fallback (`hashing-lexical-v2`), used only when
        # sentence-transformers is unavailable. Separate because that embedder's
        # cosines are on an inverted scale — see `search._semantic_cutoff`.
        lexical_min_score=float(os.getenv("LEXICAL_MIN_SCORE", "0.05")),
        # Corpus size at which `semantic_min_score` applies unmodified. 1000
        # matches the demo corpus (1049 embedded segments over 1865 segments in
        # 6 recordings) where the bands above were measured, so the threshold
        # transfers to it without re-tuning.
        semantic_floor_reference_pool=_int("SEMANTIC_FLOOR_REFERENCE_POOL", 1000),
        # Multiplier on each exhibit's fair share of the page; see
        # `search._exhibit_cap` for the formula. Tuned by measurement against the
        # live corpus (1865 segments across 6 recordings, one recording holding
        # 59% of them), at limit=10, hybrid mode — the "6 exhibits" and "2315
        # segments" quoted in earlier notes were pre-reseed figures. "no
        # regression" means the exhibit distribution came back identical to the
        # uncapped baseline:
        #
        #   share=1.00  police 10/10 presser -> 6 presser + 2 from the 911 call.
        #               But `a mission to the moon` fell 10 -> 8/10 and
        #               `woman hiding...` 9 -> 7/10: a regression on queries that
        #               already worked.
        #   share=1.25  police fixed, still moon 8/10 and football 7/10.
        #   share=1.50  police fixed, moon 9/10 — still one slot lost.
        #   share=2.00  police fixed (6 presser + 2 from the 911 call) and BOTH
        #               working queries bit-identical to baseline: moon 10/10,
        #               football 8/10, attacker 9/10. Chosen for this.
        #   share>=3.0  the monopoly returns (police 10/10 presser), past the edge.
        #
        # 2.0 wins because the cap is `ceil(limit / exhibits_matched * share)`:
        # for a query only 2 exhibits match, the allowance already reaches
        # `limit`, so the cap cannot bite and a focused query is untouched. The
        # redistribution only engages when enough exhibits matched to make the
        # fair share genuinely small, which is exactly the flooding case.
        search_exhibit_cap_share=float(os.getenv("SEARCH_EXHIBIT_CAP_SHARE", "2.0")),
        dev_default_actor=os.getenv("DEV_DEFAULT_ACTOR", "demo.investigator"),
        dev_default_role=os.getenv("DEV_DEFAULT_ROLE", "admin").lower(),
        allow_anonymous=_bool("ALLOW_ANONYMOUS", True),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = _build()
    for directory in (
        settings.data_dir,
        settings.originals_dir,
        settings.working_dir,
        settings.denoised_dir,
        settings.data_dir / "exports",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def reset_settings_cache() -> None:
    """Test helper: re-read the environment."""
    get_settings.cache_clear()
