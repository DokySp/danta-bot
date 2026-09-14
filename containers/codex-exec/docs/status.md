# 구현·검증 상태

Fact: README P1~P5의 **오프라인 구현과 로컬 엔진 검증을 완료**했다.
Docker 연결 문제는 사용자 응답 후 해소했으며 현재 환경 blocker는 없다.
당시 중단·재개와 리뷰 판정은 [decisions.md](decisions.md)에 보존했다.

## 구현 범위

README P1~P5의 Python/SQLite workflow, 전략·위험·보호·주문·대사·복구,
KIS/DART/Codex/Telegram 어댑터, 독립 paper 비교 평가, CLI/service,
Docker/Compose와 보고서·운영 문서를 구현했다. 기본은 offline이며 모델/외부 통신·
실주문 권한·스케줄·Telegram ingress는 활성화하지 않았다.

- IMPLEMENTED: 소스·설정3개·schema·migration·합성 fixture·문서.
- ENGINE_VALIDATED: 아래 합성/격리 검증 범위. 독립 검토 지적을 판정·반영하고 최종 회귀를 완료했다.
- EXTERNAL_INTEGRATION_UNVERIFIED: 실제 KIS/DART/모델/gateway와 운영 인증 연결 미실행.
- STRATEGY_UNPROVEN: 향후60+20세션 결과 없음. 합성 실험은 INCONCLUSIVE.
- LIVE_NOT_AUTHORIZED: 실제 자금/계좌·보유 인수·밤 보유·손실·실행 정책 미승인.

## 실행한 검증

| 검사 | 결과·범위 |
|---|---|
| 전체 unittest | **133개 PASS**, 2026-09-13, 실제 네트워크 금지 합성/계약 테스트 |
| source/배포 fixture credential 검사 | 36파일 PASS; Docker context는 Dockerfile/.dockerignore 포함38파일 PASS |
| 자격증명 주입 negative build | 합성 credential을 넣은 별도 context가 COPY/의존성 설치 전에 거부됨, 값 출력 없음 |
| 최종 Docker build | PASS, `danta-codex-exec:spec-20260913-final` |
| 컨테이너 실행 | UID10001/read-only/network none/capabilities 없음/no-new-privileges |
| 컨테이너 기능 | doctor VALID, 합성 FIXTURE_FILLED, 동일 요청 재사용, holding1, 보고서, 기본 serve 소켓0 |
| Compose config | 비root/read-only/호스트 포트·Docker socket·host network 없음/internal network, secret 파일 부재 기본 허용 |
| 설치 CLI 계약 probe | Codex CLI0.153.4 + loopback mock provider. 제한 도구·shell/patch 거부·임시 auth canary 미노출 |
| README/report | 원문과 byte 동일, 92개 절, SHA256 일치, 외부 script/stylesheet0 |
| 화면 | 데스크톱·390×844 모바일·목차 확장/절 이동 PASS, 문서 가로 넘침 없음 |
| 새 파일 whitespace 검사 | 74파일, 지적0 |
| 독립 검토 | gpt-5.6-luna/max 3회, 채택한 문제 수정. 마지막 국소 수정은 메인 회귀+전체133개+이미지 재검증 |

재실행 명령(새 프로젝트 디렉터리에서 Python3.12 + requirements.lock 환경):

```sh
PYTHONPATH=src python -m unittest discover -s tests -t . -p 'test_*.py' -q
PYTHONPATH=src python -m danta.safety src schemas migrations prompts README.md scripts/render_report.py tests/fixtures/offline-e2e.json pyproject.toml requirements.lock
docker build -t danta-codex-exec:local .
PYTHONPATH=src python scripts/verify_offline_image.py --image danta-codex-exec:local
```

컨테이너 검증 완료: `2026-09-13T02:52:19.246243+00:00`.
이미지 ID: `sha256:8c12f677a5ba1fe5692f523abdea3744ae20e5d33c144b638120933a7080f22c`.
호스트/이미지 공통 code ID: `4cf9800a634cb78258feb511a91ce9a03b6a5a3d8946b7732a1f669d655e5e68`.
원문 SHA256: `26d74f328784dcd461dc3aedf0f58d7e60855e41ee9179b0cc6fe8ec51be7006`.

상세 증적: [container-verification.json](container-verification.json),
[report-verification.json](report-verification.json),
[CLI probe](../tests/fixtures/adapters_cli_probe_result.json),
[S/O/E/I 추적표](acceptance.md).

## 실행하지 않은 범위

실제 외부 연결·운영 인증 mount/peer 검증·실계좌 주문·Telegram 전송,
이미지 push·NAS sync·운영 컨테이너 변경·Git commit/push는 하지 않았다.
기존 legacy 소스/설정/원장/인증 파일도 삭제하거나 변경하지 않았다.

P6은 승인된 데이터/모델·인프라에서 실제 관측 기간을 쌓아야 하며,
P7은 별도 사용자 정책과 운영 인수·보호 성능 검증이 필요하다.
이 단계들을 자동 배경 실행하거나 완료한 것으로 표시하지 않는다.
실제 공시 서식 전체 지원, 현재 운영 호가표/달력, 비용·호출량·보호 지연도 미검증이다.

검증 중 만든 합성 원장만 `/tmp/danta-preserved-synthetic-validation-iy8c3gnm/research`에
보존했다. 기본 offline 상태는 다음 실행 시 새로 생성된다. 임시 화면 검증 서버/탭은 정리했다.
