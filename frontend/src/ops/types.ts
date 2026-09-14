export type EnterpriseStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "suspended"
  | "archived";
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
  role: "owner";
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

export interface EnterpriseEntry {
  enterprise_id: number;
  enterprise_name: string;
  active: boolean;
  entry_url: string | null;
  created_at: string | null;
  updated_at: string | null;
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

// ---- 企业共创视频 ----

export type TemplateAssetUsage =
  | "character_reference"
  | "prompt_text"
  | "cover";

export interface TemplateAssetRef {
  asset_id: number;
  usage: TemplateAssetUsage;
  sort_order: number;
}

export interface TemplateAssetInfo extends TemplateAssetRef {
  asset_name: string;
  content_type: string;
  purpose: Purpose;
  mime_type: string;
}

export interface VideoTemplate {
  id: number;
  enterprise_id: number;
  name: string;
  description: string;
  prompt: string;
  first_frame_prompt: string;
  image_model: string;
  chat_model: string;
  video_model: string;
  video_provider: string;
  duration: number;
  negative_prompt: string;
  member_photo: "none" | "required" | "optional";
  member_photo_hint: string;
  first_frame_confirm: boolean;
  interaction_options: string[];
  is_active: boolean;
  sort_order: number;
  assets: TemplateAssetInfo[];
  cover_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface VideoTemplateForm {
  name: string;
  description: string;
  prompt: string;
  first_frame_prompt: string;
  image_model: string;
  chat_model: string;
  video_model: string;
  video_provider: string;
  duration: number;
  negative_prompt: string;
  member_photo: "none" | "required" | "optional";
  member_photo_hint: string;
  first_frame_confirm: boolean;
  interaction_options: string[];
  is_active: boolean;
  sort_order: number;
  assets: TemplateAssetRef[];
}

export interface CoCreationVideo {
  id: number;
  creator_id: number;
  creator_sub: string;
  creator_name: string | null;
  template_id: number | null;
  template_name: string;
  input_text: string;
  video_model: string;
  duration: number;
  status: string;
  error: string | null;
  video_url: string | null;
  created_at: string;
}
