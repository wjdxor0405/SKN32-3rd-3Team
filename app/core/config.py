from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 프로젝트 루트 경로 (app/core/config.py 기준 두 단계 위)
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # .env 파일에서 자동으로 매핑되어 채워지는 변수들
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 기본값 1일 (분 단위)
    GEMINI_API_KEY: str = ""

    # .env 파일을 최우선으로 읽어오도록 설정
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"  # .env에 클래스 정의 외의 추가 변수가 있어도 무시
    )

    # ═══════════════ RAG 설정 (여기부터 추가) ═══════════════
    # 인덱싱할 문서 출처: "db"(MySQL documents 테이블) / "files"(data/docs 폴더)
    RAG_SOURCE: str = "db"

    # 청킹: 한 청크의 최대 문자 수 / 인접 청크 간 겹침 문자 수
    CHUNK_SIZE: int = 700
    CHUNK_OVERLAP: int = 100

    # 검색: 기본으로 가져올 유사 청크 개수
    RAG_TOP_K: int = 4

    # 임베딩 백엔드: "local"(API 키 불필요, 개발용) 또는 "gemini"(실서비스)
    EMBEDDING_BACKEND: str = "local"

    # local 임베딩 벡터 차원
    LOCAL_EMBEDDING_DIMENSION: int = 384

    # gemini 임베딩 모델명과 차원
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    GEMINI_EMBEDDING_DIMENSION: int = 768

    # 임베딩 API 는 요청 1건당 최대 100개까지 받는다. 그 이하로만 설정할 것.
    GEMINI_EMBEDDING_BATCH: int = 100

    # 배치 사이 대기(초). 분당 요청 제한을 피하기 위한 여유.
    GEMINI_BATCH_DELAY: float = 2.0

    # 429 를 만났을 때 재시도 대기(초). 시도마다 배수로 늘어난다.
    GEMINI_RETRY_WAIT: int = 30

    # Gemini API 키 (팀 config에 이미 있다면 이 줄은 생략)
    GEMINI_API_KEY: str = ""

    # FAISS 인덱스 + 청크 JSON 저장 디렉터리
    INDEX_DIR: Path = BASE_DIR / "data" / "indexes"

    # (개발 단계 전용) MySQL 연결 전 문서를 읽어올 임시 폴더
    DOCS_DIR: Path = BASE_DIR / "data" / "docs"

    # 가이드 문서 폴더 (환경부·지자체 분리배출 안내문).
    # 시드 스크립트(seed_docs)의 적재 대상이자 관리자 업로드 저장처이며,
    # RAG_SOURCE=files 모드의 검색 경로이기도 하다.
    # ※ 과거 GUIDES_DIR(data/guides)와 이원화되어 있던 것을 data/guide 로 통일함 (2026-08-03)
    GUIDE_DIR: Path = BASE_DIR / "data" / "guide"

    # 법령 원문 txt 폴더 (시드 적재 스크립트가 읽는 곳)
    LAWS_DIR: Path = BASE_DIR / "data" / "laws"

    # 유사도 임계값. 이 점수 미만이면 근거 없음으로 보고 LLM을 호출하지 않는다.
    # (환각 방지 1차 장치)
    RAG_MIN_SCORE: float = 0.15

    # local 임베딩은 표면 문자열 일치만 잡아 점수 스케일이 훨씬 낮다.
    # 같은 임계값을 쓰면 전부 걸러지므로 개발용으로 따로 둔다.
    RAG_MIN_SCORE_LOCAL: float = 0.05

    # 답변 생성에 쓸 Gemini 모델
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # ═══════════════ RAG 설정 (여기까지) ═══════════════

settings = Settings()