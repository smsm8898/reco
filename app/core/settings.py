from psycopg.conninfo import make_conninfo
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    LOG_LEVEL: str = "INFO"

    # 서비스 식별 (구조적 로깅) — 배포 시 env로 주입. json_logs = (ENV != "local")
    SERVICE_NAME: str = "reco"
    SERVICE_VERSION: str = "0.1.0"
    ENV: str = "local"  # local | dev | prod

    # 구좌별 라우터 on/off
    ENABLE_RELATED: bool = False
    ENABLE_PERSONALIZED: bool = False
    ENABLE_POPULAR: bool = False

    # PostgreSQL — 컴포넌트별로 받아 접속 문자열을 조립한다 (환경별 env 주입).
    # 기본값은 docker-compose.yml의 로컬 PostgreSQL과 일치.
    PG_HOST: str = "localhost"
    PG_PORT: int = 5432
    PG_USER: str = "reco"
    PG_PASSWORD: str = "reco"
    PG_DB: str = "reco"

    # 커넥션 풀 (psycopg_pool)
    PG_POOL_MIN_SIZE: int = 2
    PG_POOL_MAX_SIZE: int = 10
    PG_STATEMENT_TIMEOUT_MS: int = 2000

    def pg_conninfo(self, dbname: str | None = None) -> str:
        """psycopg 접속 문자열. dbname을 넘기면 그 DB로(테스트/관리 접속용)."""
        return make_conninfo(
            host=self.PG_HOST,
            port=self.PG_PORT,
            user=self.PG_USER,
            password=self.PG_PASSWORD,
            dbname=dbname or self.PG_DB,
        )

    def pg_serving_kwargs(self) -> dict:
        """서빙 풀 커넥션 옵션 — statement_timeout으로 hung 쿼리를 서버측에서 취소."""
        return {"options": f"-c statement_timeout={self.PG_STATEMENT_TIMEOUT_MS}"}


settings = Settings()
