"""运营平台一期验收：认证、RBAC、素材 CRUD、额度、租户隔离和内部读取。"""

from fastapi.testclient import TestClient

from backend.settings import settings


def _headers(sub: str) -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


def _enterprise_payload(name: str, code: str) -> dict:
    return {
        "name": name,
        "credit_code": code,
        "contact_name": "测试联系人",
        "contact_phone": "13800000000",
        "contact_email": "ops@example.test",
        "description": "测试企业",
    }


def _apply_and_approve(client: TestClient, sub: str, suffix: str) -> int:
    applied = client.post(
        "/api/ops/v1/enterprise",
        json=_enterprise_payload(f"测试企业{suffix}", f"CREDIT-{suffix}"),
        headers=_headers(sub),
    )
    assert applied.status_code == 200, applied.text
    enterprise_id = applied.json()["enterprise"]["id"]
    reviewed = client.post(
        f"/api/ops/v1/admin/enterprises/{enterprise_id}/review",
        json={"status": "approved", "reason": ""},
        headers=_headers("platform-admin"),
    )
    assert reviewed.status_code == 200, reviewed.text
    return enterprise_id


def _png_bytes(extra: int = 0) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"x" * extra


def _upload_png(client: TestClient, sub: str, name: str = "logo.png"):
    return client.post(
        "/api/ops/v1/assets/file",
        data={"purpose": "brand_product", "name": "品牌 Logo", "tags": "品牌,主标"},
        files={"file": (name, _png_bytes(32), "image/png")},
        headers=_headers(sub),
    )


def test_enterprise_certification_gate_and_default_quota() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        profile = client.get("/api/ops/v1/profile", headers=_headers("owner-a"))
        assert profile.status_code == 200
        assert profile.json()["enterprise"] is None
        assert profile.json()["is_platform_admin"] is False

        applied = client.post(
            "/api/ops/v1/enterprise",
            json=_enterprise_payload("沃乐食品", "WOLE-001"),
            headers=_headers("owner-a"),
        )
        assert applied.status_code == 200
        enterprise_id = applied.json()["enterprise"]["id"]
        assert applied.json()["enterprise"]["status"] == "pending"
        assert _upload_png(client, "owner-a").status_code == 403

        assert client.get(
            "/api/ops/v1/admin/enterprises", headers=_headers("owner-a")
        ).status_code == 403
        approved = client.post(
            f"/api/ops/v1/admin/enterprises/{enterprise_id}/review",
            json={"status": "approved", "reason": ""},
            headers=_headers("platform-admin"),
        )
        assert approved.status_code == 200
        profile = client.get("/api/ops/v1/profile", headers=_headers("owner-a")).json()
        assert profile["membership"]["role"] == "owner"
        assert profile["membership"]["status"] == "active"
        assert profile["quota"]["limit_bytes"] == settings.enterprise_default_quota_mb * 1024 * 1024
        assert profile["upload_limits"]["batch_max_files"] == 20
        assert profile["upload_limits"]["batch_max_bytes"] == 1024 * 1024 * 1024

        archived = client.patch(
            f"/api/ops/v1/admin/enterprises/{enterprise_id}/status",
            json={"status": "archived", "reason": "验收归档"},
            headers=_headers("platform-admin"),
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        assert _upload_png(client, "owner-a").status_code == 403
        duplicate = client.post(
            "/api/ops/v1/enterprise",
            json=_enterprise_payload("沃乐食品", "WOLE-001"),
            headers=_headers("owner-a"),
        )
        assert duplicate.status_code == 409


def test_member_roles_are_enforced() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-a", "MEMBER")
        editor = client.post(
            "/api/ops/v1/members",
            json={"oauth_sub": "editor-a", "role": "editor"},
            headers=_headers("owner-a"),
        )
        viewer = client.post(
            "/api/ops/v1/members",
            json={"oauth_sub": "viewer-a", "role": "viewer"},
            headers=_headers("owner-a"),
        )
        assert editor.status_code == viewer.status_code == 200
        assert client.get("/api/ops/v1/assets", headers=_headers("viewer-a")).status_code == 200
        denied = client.post(
            "/api/ops/v1/assets/text",
            json={"name": "人格", "purpose": "ip_setting", "text_content": "身份：科学家"},
            headers=_headers("viewer-a"),
        )
        assert denied.status_code == 403
        allowed = client.post(
            "/api/ops/v1/assets/text",
            json={"name": "人格", "purpose": "ip_setting", "text_content": "身份：科学家"},
            headers=_headers("editor-a"),
        )
        assert allowed.status_code == 200
        owner_membership = next(
            row for row in client.get("/api/ops/v1/members", headers=_headers("owner-a")).json()
            if row["role"] == "owner"
        )
        assert client.delete(
            f"/api/ops/v1/members/{owner_membership['id']}", headers=_headers("owner-a")
        ).status_code == 409


def test_text_and_file_asset_crud_with_versions_and_quota() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-a", "CRUD")
        created_text = client.post(
            "/api/ops/v1/assets/text",
            json={
                "name": "沃乐人格",
                "purpose": "ip_setting",
                "text_content": "身份：食品营养科学家\n性格：较真、萌",
                "content_data": {"identity": "食品营养科学家"},
                "tags": ["沃乐"],
            },
            headers=_headers("owner-a"),
        )
        assert created_text.status_code == 200, created_text.text
        text_id = created_text.json()["id"]
        updated = client.patch(
            f"/api/ops/v1/assets/{text_id}",
            json={"text_content": "身份：食品营养科学家\n口头禅：天然美味，我先来一口"},
            headers=_headers("owner-a"),
        )
        assert updated.status_code == 200
        assert updated.json()["current_version"] == 2
        assert "天然美味" in updated.json()["version"]["text_content"]

        uploaded = _upload_png(client, "owner-a")
        assert uploaded.status_code == 200, uploaded.text
        file_asset = uploaded.json()
        assert file_asset["content_type"] == "image"
        assert file_asset["version"]["checksum_sha256"]
        quota = client.get("/api/ops/v1/quota", headers=_headers("owner-a")).json()
        assert quota["used_bytes"] == len(_png_bytes(32))

        content = client.get(file_asset["content_url"], headers=_headers("owner-a"))
        assert content.status_code == 200
        assert content.content == _png_bytes(32)
        assert "content-disposition" not in content.headers
        download = client.get(
            f"{file_asset['content_url']}&download=true",
            headers=_headers("owner-a"),
        )
        assert download.status_code == 200
        assert download.content == _png_bytes(32)
        assert download.headers["content-disposition"].startswith("attachment;")
        deleted = client.delete(
            f"/api/ops/v1/assets/{file_asset['id']}", headers=_headers("owner-a")
        )
        assert deleted.status_code == 200
        assert client.get(
            f"/api/ops/v1/assets/{file_asset['id']}", headers=_headers("owner-a")
        ).status_code == 404
        assert client.get("/api/ops/v1/quota", headers=_headers("owner-a")).json()["used_bytes"] == 0


def test_assets_are_tenant_isolated_and_not_public_static_files() -> None:
    from backend.app import create_app
    from backend.database import SessionLocal
    from backend.models import EnterpriseAssetVersion

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-a", "TENANT-A")
        _apply_and_approve(client, "owner-b", "TENANT-B")
        asset = _upload_png(client, "owner-a").json()
        assert client.get(
            f"/api/ops/v1/assets/{asset['id']}", headers=_headers("owner-b")
        ).status_code == 404
        assert client.delete(
            f"/api/ops/v1/assets/{asset['id']}", headers=_headers("owner-b")
        ).status_code == 404
        assert client.get(
            asset["content_url"], headers=_headers("owner-b")
        ).status_code == 404
        with SessionLocal() as db:
            version = db.get(EnterpriseAssetVersion, asset["version"]["id"])
            storage_key = version.storage_key
        assert storage_key.startswith("enterprises/")
        assert client.get(f"/api/media/{storage_key}").status_code == 404


def test_quota_and_format_limits_are_enforced_without_leaking_reservations() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        enterprise_id = _apply_and_approve(client, "owner-a", "LIMIT")
        changed = client.put(
            f"/api/ops/v1/admin/enterprises/{enterprise_id}/quota",
            json={"limit_bytes": 10},
            headers=_headers("platform-admin"),
        )
        assert changed.status_code == 200
        too_large = _upload_png(client, "owner-a")
        assert too_large.status_code == 422
        quota = client.get("/api/ops/v1/quota", headers=_headers("owner-a")).json()
        assert quota["used_bytes"] == quota["reserved_bytes"] == 0
        wrong_format = client.post(
            "/api/ops/v1/assets/file",
            data={"purpose": "ip_visual"},
            files={"file": ("fake.png", b"not-a-png", "image/png")},
            headers=_headers("owner-a"),
        )
        assert wrong_format.status_code == 422


def test_three_material_categories_enforce_their_supported_types() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-a", "CATEGORIES")

        visual_text = client.post(
            "/api/ops/v1/assets/text",
            json={"name": "错误文字", "purpose": "ip_visual", "text_content": "不应接受"},
            headers=_headers("owner-a"),
        )
        assert visual_text.status_code == 422

        setting_image = client.post(
            "/api/ops/v1/assets/file",
            data={"purpose": "ip_setting"},
            files={"file": ("persona.png", _png_bytes(), "image/png")},
            headers=_headers("owner-a"),
        )
        assert setting_image.status_code == 422

        setting_document = client.post(
            "/api/ops/v1/assets/file",
            data={"purpose": "ip_setting", "name": "IP 策划案"},
            files={"file": ("persona.pdf", b"%PDF-1.7\ncontent", "application/pdf")},
            headers=_headers("owner-a"),
        )
        assert setting_document.status_code == 200
        assert setting_document.json()["content_type"] == "document"

        visual_video = client.post(
            "/api/ops/v1/assets/file",
            data={"purpose": "ip_visual", "name": "IP 动态参考"},
            files={"file": ("motion.mp4", b"\x00\x00\x00\x18ftypisom", "video/mp4")},
            headers=_headers("owner-a"),
        )
        assert visual_video.status_code == 200
        assert visual_video.json()["content_type"] == "video"

        brand_text = client.post(
            "/api/ops/v1/assets/text",
            json={
                "name": "产品卖点",
                "purpose": "brand_product",
                "text_content": "天然配方，果香浓郁",
            },
            headers=_headers("owner-a"),
        )
        assert brand_text.status_code == 200


def test_batch_upload_validates_total_size_count_and_same_type(monkeypatch) -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        enterprise_id = _apply_and_approve(client, "owner-a", "BATCH")
        client.put(
            f"/api/ops/v1/admin/enterprises/{enterprise_id}/quota",
            json={"limit_bytes": 70},
            headers=_headers("platform-admin"),
        )
        batch_files = [
            ("files", ("a.png", _png_bytes(32), "image/png")),
            ("files", ("b.png", _png_bytes(32), "image/png")),
        ]
        over_total = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "brand_product", "material_type": "image"},
            files=batch_files,
            headers=_headers("owner-a"),
        )
        assert over_total.status_code == 422
        assert client.get("/api/ops/v1/assets", headers=_headers("owner-a")).json() == []
        quota = client.get("/api/ops/v1/quota", headers=_headers("owner-a")).json()
        assert quota["used_bytes"] == quota["reserved_bytes"] == 0

        client.put(
            f"/api/ops/v1/admin/enterprises/{enterprise_id}/quota",
            json={"limit_bytes": 1024},
            headers=_headers("platform-admin"),
        )
        uploaded = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "brand_product", "material_type": "image"},
            files=[
                ("files", ("a.png", _png_bytes(8), "image/png")),
                ("files", ("b.png", _png_bytes(16), "image/png")),
            ],
            headers=_headers("owner-a"),
        )
        assert uploaded.status_code == 200, uploaded.text
        assert [row["name"] for row in uploaded.json()] == ["a", "b"]
        assert all(row["content_type"] == "image" for row in uploaded.json())
        quota = client.get("/api/ops/v1/quota", headers=_headers("owner-a")).json()
        assert quota["used_bytes"] == len(_png_bytes(8)) + len(_png_bytes(16))
        assert quota["reserved_bytes"] == 0

        documents = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "ip_setting", "material_type": "document"},
            files=[
                ("files", ("persona-a.pdf", b"%PDF-1.7\na", "application/pdf")),
                ("files", ("persona-b.pdf", b"%PDF-1.7\nb", "application/pdf")),
            ],
            headers=_headers("owner-a"),
        )
        assert documents.status_code == 200
        assert all(row["content_type"] == "document" for row in documents.json())

        videos = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "ip_visual", "material_type": "video"},
            files=[
                ("files", ("motion-a.mp4", b"\x00\x00\x00\x18ftypisom-a", "video/mp4")),
                ("files", ("motion-b.mp4", b"\x00\x00\x00\x18ftypisom-b", "video/mp4")),
            ],
            headers=_headers("owner-a"),
        )
        assert videos.status_code == 200
        assert all(row["content_type"] == "video" for row in videos.json())

        visuals = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "ip_visual", "material_type": "image"},
            files=[
                ("files", ("visual.png", _png_bytes(), "image/png")),
                ("files", ("source.psd", b"8BPS\x00\x01source", "image/vnd.adobe.photoshop")),
            ],
            headers=_headers("owner-a"),
        )
        assert visuals.status_code == 200
        assert {row["content_type"] for row in visuals.json()} == {"image", "source"}

        asset_count = len(
            client.get("/api/ops/v1/assets", headers=_headers("owner-a")).json()
        )

        mixed = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "brand_product", "material_type": "image"},
            files=[
                ("files", ("valid.png", _png_bytes(), "image/png")),
                ("files", ("brief.pdf", b"%PDF-1.7\ncontent", "application/pdf")),
            ],
            headers=_headers("owner-a"),
        )
        assert mixed.status_code == 422
        assert (
            len(client.get("/api/ops/v1/assets", headers=_headers("owner-a")).json())
            == asset_count
        )

        monkeypatch.setattr(settings, "enterprise_batch_max_files", 1)
        too_many = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "brand_product", "material_type": "image"},
            files=batch_files,
            headers=_headers("owner-a"),
        )
        assert too_many.status_code == 422

        monkeypatch.setattr(settings, "enterprise_batch_max_files", 20)
        monkeypatch.setattr(settings, "enterprise_batch_max_mb", 0)
        over_batch_limit = client.post(
            "/api/ops/v1/assets/files",
            data={"purpose": "brand_product", "material_type": "image"},
            files=[("files", ("valid.png", _png_bytes(), "image/png"))],
            headers=_headers("owner-a"),
        )
        assert over_batch_limit.status_code == 422


def test_internal_material_rpc_requires_service_token_and_honors_enterprise_status() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        enterprise_id = _apply_and_approve(client, "owner-a", "RPC")
        text = client.post(
            "/api/ops/v1/assets/text",
            json={
                "name": "沃乐人格",
                "purpose": "ip_setting",
                "text_content": "天然美味，我先来一口",
                "tags": ["沃乐"],
            },
            headers=_headers("owner-a"),
        ).json()
        payload = {
            "enterprise_id": enterprise_id,
            "purposes": ["ip_setting"],
            "content_types": ["text"],
            "tags": ["沃乐"],
        }
        assert client.post("/api/internal/v1/materials/search", json=payload).status_code == 401
        result = client.post(
            "/api/internal/v1/materials/search",
            json=payload,
            headers={"X-Internal-Token": "internal-test-token"},
        )
        assert result.status_code == 200, result.text
        assert result.json()[0]["asset_id"] == text["id"]
        assert result.json()[0]["text_content"] == "天然美味，我先来一口"

        suspended = client.patch(
            f"/api/ops/v1/admin/enterprises/{enterprise_id}/status",
            json={"status": "suspended", "reason": "测试停用"},
            headers=_headers("platform-admin"),
        )
        assert suspended.status_code == 200
        assert client.post(
            "/api/internal/v1/materials/search",
            json=payload,
            headers={"X-Internal-Token": "internal-test-token"},
        ).status_code == 404


def test_database_platform_admin_can_be_added_and_removed() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        added = client.post(
            "/api/ops/v1/admin/platform-admins",
            json={"oauth_sub": "second-admin"},
            headers=_headers("platform-admin"),
        )
        assert added.status_code == 200
        user_id = added.json()["user_id"]
        profile = client.get("/api/ops/v1/profile", headers=_headers("second-admin"))
        assert profile.status_code == 200
        assert profile.json()["is_platform_admin"] is True
        removed = client.delete(
            f"/api/ops/v1/admin/platform-admins/{user_id}",
            headers=_headers("platform-admin"),
        )
        assert removed.status_code == 200
        assert client.get(
            "/api/ops/v1/profile", headers=_headers("second-admin")
        ).json()["is_platform_admin"] is False
