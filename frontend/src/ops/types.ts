export type EnterpriseStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "suspended"
  | "archived";
export type MemberRole = "owner" | "admin" | "editor" | "viewer";
export type Purpose =
  | "ip_setting"
  | "ip_visual"
  | "brand_product";

export interface AuthUser {
  id: number;
  oauth_sub: string;
  email: string | null;
  display_name: string | null;
  avatar_url: string | null;
}

export interface Enterprise {
  id: number;
  name: string;
  credit_code: string;
  contact_name: string;
  contact_phone: string;
  contact_email: string;
  description: string;
  status: EnterpriseStatus;
  rejection_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface Quota {
  enterprise_id: number;
  limit_bytes: number;
  used_bytes: number;
  reserved_bytes: number;
  status: string;
}

export interface UploadLimits {
  batch_max_files: number;
  batch_max_bytes: number;
  image_max_bytes: number;
  document_max_bytes: number;
  source_max_bytes: number;
  video_max_bytes: number;
}

export interface EnterpriseMembership {
  id: number;
  enterprise_id: number;
  user_id: number;
  oauth_sub: string;
  display_name: string | null;
  email: string | null;
  role: MemberRole;
  status: string;
  created_at: string;
}

export interface OpsProfile {
  user: AuthUser;
  enterprise: Enterprise | null;
  membership: EnterpriseMembership | null;
  quota: Quota | null;
  upload_limits?: UploadLimits;
  is_platform_admin: boolean;
}

export interface AssetVersion {
  id: number;
  version_no: number;
  original_filename: string | null;
  mime_type: string;
  text_content: string | null;
  content_data: Record<string, unknown>;
  size_bytes: number;
  checksum_sha256: string;
  created_at: string;
}

export interface EnterpriseAsset {
  id: number;
  enterprise_id: number;
  purpose: Purpose;
  content_type: "text" | "image" | "video" | "document" | "source";
  name: string;
  tags: string[];
  description: string;
  status: string;
  current_version: number;
  version: AssetVersion;
  content_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface EnterpriseForm {
  name: string;
  credit_code: string;
  contact_name: string;
  contact_phone: string;
  contact_email: string;
  description: string;
}

export interface PlatformAdmin {
  user_id: number;
  oauth_sub: string;
  display_name: string | null;
  email: string | null;
  source: "database" | "environment";
}
