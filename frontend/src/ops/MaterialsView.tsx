import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Download,
  Eye,
  FileArchive,
  FileText,
  Image as ImageIcon,
  Pencil,
  Plus,
  Trash2,
  UploadCloud,
  Video,
  X,
} from "lucide-react";

import {
  createTextAsset,
  deleteEnterpriseAsset,
  getCurrentQuota,
  listEnterpriseAssets,
  replaceAssetFile,
  updateAsset,
  uploadEnterpriseAssets,
} from "./api";
import type {
  EnterpriseAsset,
  OpsProfile,
  Purpose,
  Quota,
  UploadLimits,
} from "./types";

type MaterialType = "text" | "image" | "video" | "document";

interface TypeDefinition {
  value: MaterialType;
  label: string;
  accept?: string;
  formats?: string;
}

interface CategoryDefinition {
  value: Purpose;
  label: string;
  description: string;
  types: TypeDefinition[];
}

const CATEGORIES: CategoryDefinition[] = [
  {
    value: "ip_setting",
    label: "IP 设定",
    description: "角色身份、外观、性格、口头禅和行为规则",
    types: [
      { value: "text", label: "文字" },
      {
        value: "document",
        label: "文档",
        accept: ".pdf,.docx",
        formats: "PDF、DOCX，最大 50MB",
      },
    ],
  },
  {
    value: "ip_visual",
    label: "IP 形象",
    description: "角色标准形象、三视图、表情、动作和动态参考",
    types: [
      {
        value: "image",
        label: "图片",
        accept: ".jpg,.jpeg,.png,.webp,.psd",
        formats: "JPG、PNG、WebP 最大 20MB，PSD 最大 200MB",
      },
      {
        value: "video",
        label: "视频",
        accept: ".mp4,.mov,.webm",
        formats: "MP4、MOV、WebM，最大 500MB",
      },
    ],
  },
  {
    value: "brand_product",
    label: "品牌与产品",
    description: "品牌标识、产品信息、卖点和业务证明材料",
    types: [
      { value: "text", label: "文字" },
      {
        value: "image",
        label: "图片",
        accept: ".jpg,.jpeg,.png,.webp",
        formats: "JPG、PNG、WebP，最大 20MB",
      },
      {
        value: "video",
        label: "视频",
        accept: ".mp4,.mov,.webm",
        formats: "MP4、MOV、WebM，最大 500MB",
      },
      {
        value: "document",
        label: "文档",
        accept: ".pdf,.docx",
        formats: "PDF、DOCX，最大 50MB",
      },
    ],
  },
];

const DEFAULT_UPLOAD_LIMITS: UploadLimits = {
  batch_max_files: 20,
  batch_max_bytes: 1024 ** 3,
  image_max_bytes: 20 * 1024 ** 2,
  document_max_bytes: 50 * 1024 ** 2,
  source_max_bytes: 200 * 1024 ** 2,
  video_max_bytes: 500 * 1024 ** 2,
};

const TYPE_ICON = {
  text: FileText,
  image: ImageIcon,
  video: Video,
  document: FileArchive,
};

const CONTENT_LABEL: Record<EnterpriseAsset["content_type"], string> = {
  text: "文字",
  image: "图片",
  video: "视频",
  document: "文档",
  source: "图片源文件",
};

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function formatDate(value: string) {
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fileExtension(file: File) {
  return `.${file.name.split(".").pop()?.toLowerCase() ?? ""}`;
}

function fileLimit(
  file: File,
  type: TypeDefinition,
  limits: UploadLimits,
) {
  if (fileExtension(file) === ".psd") return limits.source_max_bytes;
  if (type.value === "image") return limits.image_max_bytes;
  if (type.value === "video") return limits.video_max_bytes;
  return limits.document_max_bytes;
}

function validateFileBatch(
  files: File[],
  type: TypeDefinition,
  limits: UploadLimits,
  quota: Quota | null,
) {
  if (!files.length) return "";
  if (files.length > limits.batch_max_files) {
    return `每批最多选择 ${limits.batch_max_files} 个文件`;
  }
  const allowed = new Set((type.accept ?? "").split(","));
  for (const file of files) {
    const extension = fileExtension(file);
    if (!allowed.has(extension)) {
      return `${file.name} 不是当前类型支持的格式`;
    }
    if (!file.size) return `${file.name} 是空文件`;
    const limit = fileLimit(file, type, limits);
    if (file.size > limit) {
      return `${file.name} 超过单文件上限 ${formatBytes(limit)}`;
    }
  }
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  if (totalBytes > limits.batch_max_bytes) {
    return `本批总大小不能超过 ${formatBytes(limits.batch_max_bytes)}`;
  }
  if (quota) {
    const remaining = Math.max(
      0,
      quota.limit_bytes - quota.used_bytes - quota.reserved_bytes,
    );
    if (totalBytes > remaining) {
      return `企业空间不足，当前剩余 ${formatBytes(remaining)}`;
    }
  }
  return "";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败";
}

function categoryFor(value: Purpose) {
  return CATEGORIES.find((category) => category.value === value)!;
}

function quotaPercent(quota: Quota | null) {
  if (!quota?.limit_bytes) return 0;
  return Math.min(100, (quota.used_bytes / quota.limit_bytes) * 100);
}

function belongsToType(asset: EnterpriseAsset, type: MaterialType) {
  if (type === "image") {
    return asset.content_type === "image" || asset.content_type === "source";
  }
  return asset.content_type === type;
}

function downloadUrl(contentUrl: string) {
  return `${contentUrl}${contentUrl.includes("?") ? "&" : "?"}download=true`;
}

function parsePersona(text: string) {
  const values: Record<string, string> = {};
  const keys: Record<string, string> = {
    身份: "identity",
    外观: "appearance",
    性格: "personality",
    口头禅: "catchphrase",
  };
  for (const line of text.split("\n")) {
    const match = line.match(/^([^：:]+)[：:]\s*(.+)$/);
    const key = match ? keys[match[1].trim()] : undefined;
    if (match && key) values[key] = match[2].trim();
  }
  return values;
}

function StorageSummary({ quota }: { quota: Quota | null }) {
  if (!quota) return null;
  return (
    <div className="materials-quota">
      <div>
        <span>存储空间</span>
        <strong>
          {formatBytes(quota.used_bytes)} / {formatBytes(quota.limit_bytes)}
        </strong>
      </div>
      <i aria-hidden="true">
        <b style={{ width: `${quotaPercent(quota)}%` }} />
      </i>
    </div>
  );
}

function TextEntry({
  category,
  busy,
  onSubmit,
}: {
  category: CategoryDefinition;
  busy: boolean;
  onSubmit: (text: string) => Promise<boolean>;
}) {
  const [text, setText] = useState("");
  const isPersona = category.value === "ip_setting";
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (await onSubmit(text)) setText("");
  };
  return (
    <form className="material-text-entry" onSubmit={submit}>
      <textarea
        required
        maxLength={5000}
        aria-label={`${category.label}文字内容`}
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder={
          isPersona
            ? "身份：食品营养科学家\n外观：健美的身材\n性格：较真、萌、偶尔犯迷糊\n口头禅：天然美味，我先来一口"
            : "填写品牌介绍、产品卖点、规格参数或使用要求"
        }
      />
      <footer>
        <span>{text.length} / 5000</span>
        <button className="primary" disabled={busy || !text.trim()}>
          <Plus size={16} />
          {busy ? "保存中..." : "保存文字"}
        </button>
      </footer>
    </form>
  );
}

function FileEntry({
  category,
  type,
  busy,
  quota,
  uploadLimits,
  onSubmit,
}: {
  category: CategoryDefinition;
  type: TypeDefinition;
  busy: boolean;
  quota: Quota | null;
  uploadLimits: UploadLimits;
  onSubmit: (files: File[]) => Promise<boolean>;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [inputKey, setInputKey] = useState(0);
  const Icon = TYPE_ICON[type.value];
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  const validationError = validateFileBatch(files, type, uploadLimits, quota);
  const remaining = quota
    ? Math.max(0, quota.limit_bytes - quota.used_bytes - quota.reserved_bytes)
    : null;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!files.length || validationError) return;
    if (await onSubmit(files)) {
      setFiles([]);
      setInputKey((value) => value + 1);
    }
  };
  return (
    <form
      className="material-upload-entry"
      onSubmit={submit}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        setFiles(Array.from(event.dataTransfer.files ?? []));
      }}
    >
      <div className="material-upload-selection">
        <label className="material-file-picker">
          <input
            key={inputKey}
            multiple
            required
            type="file"
            accept={type.accept}
            onChange={(event) =>
              setFiles(Array.from(event.target.files ?? []))
            }
          />
          <span className="material-upload-icon">
            <UploadCloud size={24} />
          </span>
          <span>
            <strong>
              {files.length
                ? `已选择 ${files.length} 个${type.label}`
                : `批量选择${type.label}`}
            </strong>
            <small>
              {files.length
                ? `总计 ${formatBytes(totalBytes)} · 点击重新选择`
                : `${category.label} · ${type.formats}`}
            </small>
          </span>
        </label>
        <div className="material-batch-limits">
          <span>
            每批最多 {uploadLimits.batch_max_files} 个 / {formatBytes(uploadLimits.batch_max_bytes)}
          </span>
          {remaining !== null && <span>企业剩余 {formatBytes(remaining)}</span>}
        </div>
        {files.length > 0 && (
          <div className="material-file-list">
            {files.map((file, index) => (
              <div key={`${file.name}-${file.size}-${index}`}>
                <span title={file.name}>
                  <strong>{file.name}</strong>
                  <small>{formatBytes(file.size)}</small>
                </span>
                <button
                  type="button"
                  aria-label={`移除 ${file.name}`}
                  title="从本批移除"
                  onClick={() =>
                    setFiles((current) =>
                      current.filter((_, fileIndex) => fileIndex !== index),
                    )
                  }
                >
                  <X size={15} />
                </button>
              </div>
            ))}
          </div>
        )}
        {validationError && (
          <div className="upload-validation-error">{validationError}</div>
        )}
      </div>
      <button
        className="primary"
        disabled={busy || !files.length || Boolean(validationError)}
      >
        <Icon size={16} />
        {busy
          ? `正在上传 ${files.length} 个...`
          : files.length
            ? `上传 ${files.length} 个${type.label}`
            : `上传${type.label}`}
      </button>
    </form>
  );
}

function AssetPreview({ asset }: { asset: EnterpriseAsset }) {
  if (asset.content_type === "text") {
    return <p className="material-text-preview">{asset.version.text_content}</p>;
  }
  if (asset.content_type === "image" && asset.content_url) {
    return <img loading="lazy" src={asset.content_url} alt={asset.name} />;
  }
  if (asset.content_type === "video" && asset.content_url) {
    return <video controls preload="metadata" src={asset.content_url} />;
  }
  const extension = asset.version.original_filename?.split(".").pop()?.toUpperCase();
  return (
    <div className="material-file-preview">
      <FileArchive size={28} />
      <strong>{extension || "FILE"}</strong>
      <span>{asset.version.original_filename}</span>
    </div>
  );
}

function AssetCard({
  asset,
  canEdit,
  onEdit,
  onDelete,
}: {
  asset: EnterpriseAsset;
  canEdit: boolean;
  onEdit: () => void;
  onDelete: () => Promise<unknown>;
}) {
  return (
    <article className={`material-card material-${asset.content_type}`}>
      <div className="material-preview">
        <AssetPreview asset={asset} />
      </div>
      <div className="material-card-body">
        <div className="material-card-title">
          <strong>{asset.name}</strong>
          <span>{CONTENT_LABEL[asset.content_type]}</span>
        </div>
        <p>
          v{asset.current_version}
          {asset.content_type !== "text" &&
            ` · ${formatBytes(asset.version.size_bytes)}`}
          {` · ${formatDate(asset.updated_at)}`}
        </p>
        <div className="material-actions">
          {asset.content_type === "image" && asset.content_url && (
            <a
              href={asset.content_url}
              target="_blank"
              rel="noreferrer"
              title="预览原图"
            >
              <Eye size={15} />
              预览
            </a>
          )}
          {asset.content_url && (
            <a href={downloadUrl(asset.content_url)} title="下载素材">
              <Download size={15} />
              下载
            </a>
          )}
          {canEdit && (
            <button onClick={onEdit} title="编辑素材">
              <Pencil size={15} />
              编辑
            </button>
          )}
          {canEdit && (
            <button
              className="danger-text"
              title="删除素材"
              onClick={() => {
                if (window.confirm(`确定删除“${asset.name}”吗？`)) {
                  void onDelete();
                }
              }}
            >
              <Trash2 size={15} />
              删除
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

function AssetEditDialog({
  asset,
  onClose,
  onSaved,
}: {
  asset: EnterpriseAsset;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [text, setText] = useState(asset.version.text_content ?? "");
  const [replacement, setReplacement] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const category = categoryFor(asset.purpose);
  const materialType: MaterialType =
    asset.content_type === "source"
      ? "image"
      : (asset.content_type as MaterialType);
  const accept = category.types.find((item) => item.value === materialType)?.accept;
  return (
    <div className="ops-modal-backdrop" onMouseDown={onClose}>
      <div
        className="ops-modal small"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-head">
          <h3>{asset.content_type === "text" ? "编辑文字" : "替换文件"}</h3>
          <button className="icon-button" aria-label="关闭" onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        {error && <div className="ops-alert error modal-alert">{error}</div>}
        <form
          className="focused-edit-form"
          onSubmit={async (event) => {
            event.preventDefault();
            setBusy(true);
            setError("");
            try {
              if (asset.content_type === "text") {
                await updateAsset(asset.id, { text_content: text });
              } else if (replacement) {
                await replaceAssetFile(asset.id, replacement);
              }
              await onSaved();
            } catch (reason) {
              setError(errorMessage(reason));
            } finally {
              setBusy(false);
            }
          }}
        >
          {asset.content_type === "text" ? (
            <textarea
              required
              rows={11}
              maxLength={5000}
              aria-label="文字内容"
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          ) : (
            <label className="replacement-picker">
              <span>选择新的{CONTENT_LABEL[asset.content_type]}</span>
              <input
                required
                type="file"
                accept={accept}
                onChange={(event) =>
                  setReplacement(event.target.files?.[0] ?? null)
                }
              />
              <small>{replacement?.name ?? asset.version.original_filename}</small>
            </label>
          )}
          <div className="form-actions">
            <button type="button" onClick={onClose}>
              取消
            </button>
            <button
              className="primary"
              disabled={
                busy || (asset.content_type !== "text" && !replacement)
              }
            >
              {busy ? "保存中..." : "保存"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function MaterialsView({ profile }: { profile: OpsProfile }) {
  const [assets, setAssets] = useState<EnterpriseAsset[]>([]);
  const [quota, setQuota] = useState<Quota | null>(profile.quota);
  const [purpose, setPurpose] = useState<Purpose>("ip_setting");
  const [materialType, setMaterialType] = useState<MaterialType>("text");
  const [editing, setEditing] = useState<EnterpriseAsset | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const uploadLimits = profile.upload_limits ?? DEFAULT_UPLOAD_LIMITS;
  const canEdit = ["owner", "admin", "editor"].includes(
    profile.membership?.role ?? "",
  );
  const category = categoryFor(purpose);
  const typeDefinition = category.types.find(
    (item) => item.value === materialType,
  )!;
  const currentAssets = useMemo(
    () =>
      assets.filter(
        (asset) =>
          asset.purpose === purpose && belongsToType(asset, materialType),
      ),
    [assets, materialType, purpose],
  );
  const load = useCallback(async () => {
    try {
      const [assetRows, currentQuota] = await Promise.all([
        listEnterpriseAssets(),
        getCurrentQuota(),
      ]);
      setAssets(assetRows);
      setQuota(currentQuota);
      setError("");
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await load();
      return true;
    } catch (reason) {
      setError(errorMessage(reason));
      return false;
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="materials-view">
      <div className="materials-topbar">
        <div>
          <span className="section-kicker">素材中心</span>
          <h2>企业素材库</h2>
          <p>{profile.enterprise?.name} · 共 {assets.length} 项素材</p>
        </div>
        <StorageSummary quota={quota} />
      </div>

      <nav className="material-category-nav" aria-label="素材分类">
        {CATEGORIES.map((item) => {
          const count = assets.filter(
            (asset) => asset.purpose === item.value,
          ).length;
          return (
            <button
              key={item.value}
              className={purpose === item.value ? "active" : ""}
              onClick={() => {
                setPurpose(item.value);
                setMaterialType(item.types[0].value);
                setError("");
              }}
            >
              <span>
                <strong>{item.label}</strong>
                <small>{item.description}</small>
              </span>
              <b>{count}</b>
            </button>
          );
        })}
      </nav>

      <div className="material-workspace">
        <header className="material-purpose-head">
          <div>
            <h3>{category.label}</h3>
            <p>{category.description}</p>
          </div>
          <div className="material-type-tabs" aria-label="素材类型">
            {category.types.map((item) => {
              const Icon = TYPE_ICON[item.value];
              return (
                <button
                  key={item.value}
                  className={materialType === item.value ? "active" : ""}
                  onClick={() => {
                    setMaterialType(item.value);
                    setError("");
                  }}
                >
                  <Icon size={15} />
                  {item.label}
                </button>
              );
            })}
          </div>
        </header>

        {error && <div className="ops-alert error">{error}</div>}
        {canEdit && materialType === "text" && (
          <TextEntry
            key={purpose}
            category={category}
            busy={busy}
            onSubmit={(text) =>
              run(() =>
                createTextAsset({
                  name: purpose === "ip_setting" ? "IP 设定" : "品牌与产品说明",
                  purpose,
                  text_content: text,
                  content_data:
                    purpose === "ip_setting" ? parsePersona(text) : {},
                  tags: [],
                  description: "",
                }),
              )
            }
          />
        )}
        {canEdit && materialType !== "text" && (
          <FileEntry
            key={`${purpose}-${materialType}`}
            category={category}
            type={typeDefinition}
            busy={busy}
            quota={quota}
            uploadLimits={uploadLimits}
            onSubmit={(files) =>
              run(() =>
                uploadEnterpriseAssets(
                  files,
                  purpose,
                  materialType as "image" | "video" | "document",
                ),
              )
            }
          />
        )}

        <div className="saved-materials-head">
          <h3>已保存的{typeDefinition.label}</h3>
          <span>{currentAssets.length} 项</span>
        </div>
        {currentAssets.length ? (
          <div className="material-grid">
            {currentAssets.map((asset) => (
              <AssetCard
                key={asset.id}
                asset={asset}
                canEdit={canEdit}
                onEdit={() => setEditing(asset)}
                onDelete={() => run(() => deleteEnterpriseAsset(asset.id))}
              />
            ))}
          </div>
        ) : (
          <div className="materials-empty">
            <span className="empty-icon">
              {(() => {
                const Icon = TYPE_ICON[materialType];
                return <Icon size={22} />;
              })()}
            </span>
            <strong>还没有{typeDefinition.label}素材</strong>
            <span>
              {canEdit
                ? "从上方录入或上传第一项素材"
                : "企业管理员尚未添加这类素材"}
            </span>
          </div>
        )}
      </div>

      {editing && (
        <AssetEditDialog
          asset={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await load();
          }}
        />
      )}
    </section>
  );
}
