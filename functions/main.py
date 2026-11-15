"""웹에서 요청(jobs 문서)이 생기면 GitHub Actions 수집 워크플로를 바로 실행한다."""
import requests
from firebase_admin import initialize_app
from firebase_functions import firestore_fn, options, params

initialize_app()
options.set_global_options(max_instances=1)

GH_DISPATCH_TOKEN = params.SecretParam("GH_DISPATCH_TOKEN")  # Actions 실행 권한만 있는 GitHub 토큰
GH_REPO = params.StringParam("GH_REPO")                      # 예: red9keep/ytb-web


@firestore_fn.on_document_created(document="jobs/{jobId}", secrets=[GH_DISPATCH_TOKEN])
def dispatch_collect(event: firestore_fn.Event[firestore_fn.DocumentSnapshot | None]) -> None:
    snap = event.data
    if snap is None:
        return
    job = snap.to_dict() or {}
    r = requests.post(
        f"https://api.github.com/repos/{GH_REPO.value}/actions/workflows/collect.yml/dispatches",
        headers={
            "Authorization": f"Bearer {GH_DISPATCH_TOKEN.value}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": "main", "inputs": {"collect_all": "true" if job.get("type") == "collect_all" else "false"}},
        timeout=15,
    )
    snap.reference.update({
        "dispatch": "ok" if r.status_code == 204 else f"error {r.status_code}: {r.text[:200]}",
    })
