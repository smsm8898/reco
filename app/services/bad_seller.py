"""공통 신호 — bad_seller (전역 차단 셀러 set).

정의 SSOT = `service_db.producer_service_quality`(result=0) 셀러. 셀러당 1행 스냅샷을 주기적으로
재평가하는 신호라 품질이 개선되면 차단에서 자동으로 빠진다(self-healing).
"""


from psycopg_pool import AsyncConnectionPool

# 정의 SSOT — result 0 = 최하 등급(10·20 은 정상). 월 단위 재평가되는 현재-품질 스냅샷이라
# 제재 미러 같은 stale gap 이 없다.
_BAD_SELLERS_SQL = """
    SELECT seller_seq
    FROM service_db.producer_service_quality
    WHERE result = 0
"""


async def get_bad_sellers(pool: AsyncConnectionPool) -> set[int]:
    """전역 차단 셀러 seller_seq set.

    빈 set 은 정상 상태(차단 셀러 없음)이며 조회 실패와 구분된다 — 캐시를 도입할 때 이 구분이
    negative caching(빈 결과도 캐싱)의 전제가 된다.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(_BAD_SELLERS_SQL)
        rows = await cur.fetchall()
    return {seller_seq for (seller_seq,) in rows}
