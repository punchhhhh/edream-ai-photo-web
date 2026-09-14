import type {
  CoCreationVideo,
  Enterprise,
  EnterpriseAsset,
  EnterpriseEntry,
  EnterpriseConsumerGrant,
  EnterpriseForm,
  NewApiModelCatalog,
  OpsProfile,
  PlatformAdmin,
  Purpose,
  Quota,
  VideoTemplate,
  VideoTemplateForm,
} from "./types";
import { isExplicitlyLoggedOut } from "../authNavigation";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "include",
  });
  if (
    response.status === 401 &&
    !path.startsWith("/api/auth/") &&
    !isExplicitlyLoggedOut()
  ) {
    window.location.href = `/api/auth/login?next=${encodeURIComponent("/ops")}`;
    throw new Error("登录已失效，正在重新登录");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail ?? `请求失败：${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

const json = (method: string, payload: unknown): RequestInit => ({
  method,
  body: JSON.stringify(payload),
});

export const fetchOpsProfile = () => request<OpsProfile>("/api/ops/v1/profile");
export const logoutOps = () =>
  request<{ ok: boolean; sso_logout_url: string | null }>("/api/auth/logout", {
    method: "POST",
  });
export const applyEnterprise = (payload: EnterpriseForm) =>
  request<OpsProfile>("/api/ops/v1/enterprise", json("POST", payload));
export const updateEnterprise = (payload: EnterpriseForm) =>
  request<OpsProfile>("/api/ops/v1/enterprise", json("PUT", payload));
export const cancelEnterprise = () =>
  request<{ ok: boolean }>("/api/ops/v1/enterprise", { method: "DELETE" });

export const listEnterpriseAssets = (purpose?: Purpose) =>
  request<EnterpriseAsset[]>(
    `/api/ops/v1/assets${purpose ? `?purpose=${purpose}` : ""}`,
  );
export const getCurrentQuota = () => request<Quota>("/api/ops/v1/quota");
export const createTextAsset = (payload: {
  name: string;
  purpose: Purpose;
  text_content: string;
  content_data?: Record<string, unknown>;
  tags: string[];
  description: string;
}) =>
  request<EnterpriseAsset>("/api/ops/v1/assets/text", json("POST", payload));
export const uploadEnterpriseAsset = (
  file: File,
  purpose: Purpose,
  name: string,
  tags: string,
  description: string,
) => {
  const form = new FormData();
  form.append("file", file);
  form.append("purpose", purpose);
  form.append("name", name);
  form.append("tags", tags);
  form.append("description", description);
  return request<EnterpriseAsset>("/api/ops/v1/assets/file", {
    method: "POST",
    body: form,
  });
};
export const uploadEnterpriseAssets = (
  files: File[],
  purpose: Purpose,
  materialType: "image" | "video" | "document",
) => {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  form.append("purpose", purpose);
  form.append("material_type", materialType);
  return request<EnterpriseAsset[]>("/api/ops/v1/assets/files", {
    method: "POST",
    body: form,
  });
};
export const updateAsset = (
  id: number,
  payload: Partial<
    Pick<EnterpriseAsset, "name" | "purpose" | "tags" | "description">
  > & {
    text_content?: string;
  },
) =>
  request<EnterpriseAsset>(`/api/ops/v1/assets/${id}`, json("PATCH", payload));
export const replaceAssetFile = (id: number, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return request<EnterpriseAsset>(`/api/ops/v1/assets/${id}/file`, {
    method: "POST",
    body: form,
  });
};
export const deleteEnterpriseAsset = (id: number) =>
  request<{ ok: boolean }>(`/api/ops/v1/assets/${id}`, { method: "DELETE" });

export const getEnterpriseEntry = () =>
  request<EnterpriseEntry>("/api/ops/v1/enterprise-entry");
export const createEnterpriseEntry = () =>
  request<EnterpriseEntry>("/api/ops/v1/enterprise-entry", { method: "POST" });
export const updateEnterpriseEntry = (payload: {
  grant_ttl_hours?: number;
  video_limit?: number;
}) =>
  request<EnterpriseEntry>("/api/ops/v1/enterprise-entry", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
export const disableEnterpriseEntry = () =>
  request<EnterpriseEntry>("/api/ops/v1/enterprise-entry", { method: "DELETE" });

export const listCocreationGrants = () =>
  request<EnterpriseConsumerGrant[]>("/api/ops/v1/cocreation-grants");
export const approveCocreationGrant = (id: number) =>
  request<EnterpriseConsumerGrant>(
    `/api/ops/v1/cocreation-grants/${id}/approve`,
    json("POST", {}),
  );
export const rejectCocreationGrant = (id: number, reason = "") =>
  request<EnterpriseConsumerGrant>(
    `/api/ops/v1/cocreation-grants/${id}/reject`,
    json("POST", { reason }),
  );
export const revokeCocreationGrant = (id: number, reason = "") =>
  request<EnterpriseConsumerGrant>(
    `/api/ops/v1/cocreation-grants/${id}/revoke`,
    json("POST", { reason }),
  );
export const renewCocreationGrant = (id: number) =>
  request<EnterpriseConsumerGrant>(
    `/api/ops/v1/cocreation-grants/${id}/renew`,
    json("POST", {}),
  );

export const listEnterprises = (status = "all") =>
  request<Enterprise[]>(
    `/api/ops/v1/admin/enterprises?status=${encodeURIComponent(status)}`,
  );
export const reviewEnterprise = (
  id: number,
  status: "approved" | "rejected",
  reason = "",
) =>
  request<Enterprise>(
    `/api/ops/v1/admin/enterprises/${id}/review`,
    json("POST", { status, reason }),
  );
export const changeEnterpriseStatus = (
  id: number,
  status: "approved" | "suspended" | "archived",
  reason = "",
) =>
  request<Enterprise>(
    `/api/ops/v1/admin/enterprises/${id}/status`,
    json("PATCH", { status, reason }),
  );
export const getEnterpriseQuota = (id: number) =>
  request<Quota>(`/api/ops/v1/admin/enterprises/${id}/quota`);
export const updateEnterpriseQuota = (id: number, limit_bytes: number) =>
  request<Quota>(
    `/api/ops/v1/admin/enterprises/${id}/quota`,
    json("PUT", { limit_bytes }),
  );
export const listPlatformAdmins = () =>
  request<PlatformAdmin[]>("/api/ops/v1/admin/platform-admins");
export const addPlatformAdmin = (oauth_sub: string) =>
  request<PlatformAdmin>(
    "/api/ops/v1/admin/platform-admins",
    json("POST", { oauth_sub }),
  );
export const deletePlatformAdmin = (userId: number) =>
  request<{ ok: boolean }>(`/api/ops/v1/admin/platform-admins/${userId}`, {
    method: "DELETE",
  });

// ---- 企业共创视频 ----
export const listVideoTemplates = () =>
  request<VideoTemplate[]>("/api/ops/v1/video-templates");
export const listNewApiModels = () =>
  request<NewApiModelCatalog>("/api/ops/v1/new-api-models");
export const createVideoTemplate = (payload: VideoTemplateForm) =>
  request<VideoTemplate>("/api/ops/v1/video-templates", json("POST", payload));
export const updateVideoTemplate = (
  id: number,
  payload: Partial<VideoTemplateForm>,
) =>
  request<VideoTemplate>(
    `/api/ops/v1/video-templates/${id}`,
    json("PATCH", payload),
  );
export const deleteVideoTemplate = (id: number) =>
  request<{ ok: boolean }>(`/api/ops/v1/video-templates/${id}`, {
    method: "DELETE",
  });
export const listCoCreationVideos = () =>
  request<CoCreationVideo[]>("/api/ops/v1/cocreation/videos");
