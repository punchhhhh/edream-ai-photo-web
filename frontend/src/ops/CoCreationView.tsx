import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createVideoTemplate,
  deleteVideoTemplate,
  listCoCreationVideos,
  listEnterpriseAssets,
  listVideoTemplates,
  updateVideoTemplate,
} from "./api";
import type {
  CoCreationVideo,
  EnterpriseAsset,
  OpsProfile,
  Purpose,
  TemplateAssetRef,
  VideoTemplate,
  VideoTemplateForm,
} from "./types";

const EMPTY_FORM: VideoTemplateForm = {
  name: "",
  description: "",
  prompt: "",
  first_frame_prompt: "",
  image_model: "",
  chat_model: "",
  video_model: "",
  video_provider: "video_generations",
  duration: 5,
  negative_prompt: "",
  member_photo: "none",
  member_photo_hint: "",
  first_frame_confirm: true,
  interaction_options: [],
  is_active: true,
  sort_order: 0,
  assets: [],
};

const STATUS_LABEL: Record<string, string> = {
  pending: "排队中",
  generating_video: "生成中",
  completed: "已完成",
  failed: "失败",
};

const MEMBER_PHOTO_LABEL: Record<VideoTemplateForm["member_photo"], string> = {
  none: "不出镜",
  required: "需出镜",
  optional: "可出镜",
};

const PURPOSE_LABEL: Record<Purpose, string> = {
  ip_setting: "IP 设定",
  ip_visual: "IP 形象",
  brand_product: "品牌/产品",
};

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败";
}

function parseOptions(raw: string): string[] {
  return raw
    .split(/[,，、\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 8);
}

function TemplateForm({
  initial,
  onSave,
  onClose,
}: {
  initial: { form: VideoTemplateForm; id: number | null };
  onSave: (form: VideoTemplateForm, id: number | null) => Promise<void>;
  onClose: () => void;
}) {
  const [form, setForm] = useState<VideoTemplateForm>(initial.form);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [assets, setAssets] = useState<EnterpriseAsset[]>([]);
  const [assetsError, setAssetsError] = useState("");
  // 素材绑定选择:形象参考图(多选)、特征文字(多选)、封面(单选)
  const [characterIds, setCharacterIds] = useState<number[]>(
    initial.form.assets
      .filter((a) => a.usage === "character_reference")
      .map((a) => a.asset_id),
  );
  const [promptTextIds, setPromptTextIds] = useState<number[]>(
    initial.form.assets.filter((a) => a.usage === "prompt_text").map((a) => a.asset_id),
  );
  const [coverId, setCoverId] = useState<number | null>(
    initial.form.assets.find((a) => a.usage === "cover")?.asset_id ?? null,
  );

  useEffect(() => {
    listEnterpriseAssets()
      .then((rows) => {
        setAssets(rows.filter((row) => row.status === "active"));
      })
      .catch((err) => setAssetsError(errorMessage(err)));
  }, []);

  const imageAssets = useMemo(
    () => assets.filter((row) => row.content_type === "image"),
    [assets],
  );
  const textAssets = useMemo(
    () => assets.filter((row) => row.content_type === "text"),
    [assets],
  );

  const set = <K extends keyof VideoTemplateForm>(key: K, value: VideoTemplateForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const toggleId = (list: number[], id: number) =>
    list.includes(id) ? list.filter((item) => item !== id) : [...list, id];

  const buildAssets = (): TemplateAssetRef[] => [
    ...characterIds.slice(0, 3).map((asset_id, index) => ({
      asset_id,
      usage: "character_reference" as const,
      sort_order: index,
    })),
    ...promptTextIds.slice(0, 4).map((asset_id, index) => ({
      asset_id,
      usage: "prompt_text" as const,
      sort_order: index,
    })),
    ...(coverId
      ? [{ asset_id: coverId, usage: "cover" as const, sort_order: 0 }]
      : []),
  ];

  const renderPicker = (
    title: string,
    hint: string,
    rows: EnterpriseAsset[],
    checkedIds: number[],
    onToggle: (id: number) => void,
    single = false,
  ) => (
    <div className="asset-picker span-2">
      <strong>{title}</strong>
      <small>{hint}</small>
      {rows.length === 0 ? (
        <small className="muted">素材库中还没有可用素材,请先到「企业素材」上传</small>
      ) : (
        <div className="asset-picker-options">
          {rows.map((row) => {
            const checked = single ? checkedIds[0] === row.id : checkedIds.includes(row.id);
            return (
              <label key={row.id} className={`asset-card ${checked ? "checked" : ""}`}>
                <input
                  type={single ? "radio" : "checkbox"}
                  name={single ? "cover-asset" : undefined}
                  checked={checked}
                  onChange={() => onToggle(row.id)}
                />
                {row.content_type === "image" && row.content_url ? (
                  <img
                    className="asset-card-thumb"
                    src={row.content_url}
                    alt={row.name}
                    loading="lazy"
                  />
                ) : row.content_type === "text" ? (
                  <span className="asset-card-thumb text">
                    {(row.version.text_content ?? "").replace(/\s+/g, " ").trim().slice(0, 48) ||
                      "文字素材"}
                  </span>
                ) : (
                  <span className="asset-card-thumb placeholder">🗂️</span>
                )}
                <span className="asset-card-name" title={row.name}>
                  {row.name}
                </span>
                <small>{PURPOSE_LABEL[row.purpose] ?? row.purpose}</small>
              </label>
            );
          })}
          {single && (
            <label className={`asset-card ${checkedIds.length === 0 ? "checked" : ""}`}>
              <input
                type="radio"
                name="cover-asset"
                checked={checkedIds.length === 0}
                onChange={() => onToggle(-1)}
              />
              <span className="asset-card-thumb placeholder">🚫</span>
              <span className="asset-card-name">不设封面</span>
              <small>成员端显示默认占位</small>
            </label>
          )}
        </div>
      )}
    </div>
  );

  return (
    <div className="ops-modal-backdrop" onMouseDown={onClose}>
      <div className="ops-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>{initial.id ? "编辑模版" : "新建模版"}</h3>
          <button aria-label="关闭" onClick={onClose}>
            ×
          </button>
        </div>
        {error && <div className="ops-alert error">{error}</div>}
        {assetsError && <div className="ops-alert error">{assetsError}</div>}
        <form
          className="ops-form"
          onSubmit={async (event) => {
            event.preventDefault();
            setBusy(true);
            setError("");
            try {
              await onSave({ ...form, assets: buildAssets() }, initial.id);
              onClose();
            } catch (err) {
              setError(errorMessage(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            模版名称
            <input
              required
              maxLength={100}
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="如:和品牌 IP 合拍开工舞"
            />
          </label>
          <label>
            排序(小的在前)
            <input
              type="number"
              min={0}
              max={9999}
              value={form.sort_order}
              onChange={(e) => set("sort_order", Number(e.target.value))}
            />
          </label>
          <label className="span-2">
            模版说明(成员可见)
            <input
              maxLength={500}
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              placeholder="一句话说明这个互动场景"
            />
          </label>
          <label className="span-2">
            画面与动作要求(拼在成员创意前面,驱动视频生成)
            <textarea
              rows={3}
              maxLength={4000}
              value={form.prompt}
              onChange={(e) => set("prompt", e.target.value)}
              placeholder="如:和企业 IP 一起跳舞,欢乐氛围,镜头缓慢拉远"
            />
          </label>
          <label className="span-2">
            合拍首帧构图(可选;留空用上面的画面要求合成首帧)
            <textarea
              rows={2}
              maxLength={4000}
              value={form.first_frame_prompt}
              onChange={(e) => set("first_frame_prompt", e.target.value)}
              placeholder="如:两人并肩站在店门口,IP 在左侧比心,人物在右侧微笑挥手"
            />
          </label>
          <label>
            成员出镜
            <select
              value={form.member_photo}
              onChange={(e) =>
                set("member_photo", e.target.value as VideoTemplateForm["member_photo"])
              }
            >
              <option value="none">不出镜</option>
              <option value="required">必须出镜(上传照片)</option>
              <option value="optional">可选出镜</option>
            </select>
          </label>
          <label>
            出镜引导文案(成员可见)
            <input
              maxLength={200}
              value={form.member_photo_hint}
              onChange={(e) => set("member_photo_hint", e.target.value)}
              placeholder="如:请上传一张清晰的正面照"
            />
          </label>
          <label>
            首帧确认
            <select
              value={form.first_frame_confirm ? "confirm" : "auto"}
              onChange={(e) => set("first_frame_confirm", e.target.value === "confirm")}
            >
              <option value="confirm">需成员确认首帧后再生成视频</option>
              <option value="auto">自动连出(合成后直接生成视频)</option>
            </select>
          </label>
          <label>
            图像模型(首帧合成;留空用企业主默认配置)
            <input
              maxLength={200}
              value={form.image_model}
              onChange={(e) => set("image_model", e.target.value)}
              placeholder="如:gpt-image-2"
            />
          </label>
          <label className="span-2">
            剧情选项(可选,成员点选即用;用逗号分隔,最多 8 个)
            <input
              value={form.interaction_options.join("，")}
              onChange={(e) => set("interaction_options", parseOptions(e.target.value))}
              placeholder="如:一起比心，跳一段开工舞，举产品合影"
            />
          </label>
          {renderPicker(
            "IP 形象参考图(进首帧合成,最多 3 张)",
            "建议上传标准立绘/多视角图;合成时与成员照片一起交给图像模型",
            imageAssets,
            characterIds,
            (id) => setCharacterIds((prev) => toggleId(prev, id).slice(0, 3)),
          )}
          {renderPicker(
            "特征文字(注入提示词,最多 4 条)",
            "IP 外形/配色/性格等要点,首帧合成与视频生成都会带上",
            textAssets,
            promptTextIds,
            (id) => setPromptTextIds((prev) => toggleId(prev, id).slice(0, 4)),
          )}
          {renderPicker(
            "模版封面(单选)",
            "成员端模版卡片展示的封面图",
            imageAssets,
            coverId ? [coverId] : [],
            (id) => setCoverId(id === -1 ? null : id),
            true,
          )}
          <label>
            视频模型
            <input
              required
              maxLength={200}
              value={form.video_model}
              onChange={(e) => set("video_model", e.target.value)}
              placeholder="如:seedance-1-lite"
            />
          </label>
          <label>
            文本模型(可选,提供 AI 拓展)
            <input
              maxLength={200}
              value={form.chat_model}
              onChange={(e) => set("chat_model", e.target.value)}
              placeholder="留空则成员端不展示 AI 拓展"
            />
          </label>
          <label>
            接口风格
            <select
              value={form.video_provider}
              onChange={(e) => set("video_provider", e.target.value)}
            >
              <option value="video_generations">任务式(/v1/video/generations)</option>
              <option value="openai_videos">Sora 风格(/v1/videos)</option>
            </select>
          </label>
          <label>
            时长(秒)
            <input
              type="number"
              min={1}
              max={60}
              value={form.duration}
              onChange={(e) => set("duration", Number(e.target.value))}
            />
          </label>
          <label className="span-2">
            负向提示词(可选)
            <input
              maxLength={2000}
              value={form.negative_prompt}
              onChange={(e) => set("negative_prompt", e.target.value)}
              placeholder="不希望出现的画面要素"
            />
          </label>
          <label className="span-2 checkbox-line">
            <input
              type="checkbox"
              checked={form.is_active}
              onChange={(e) => set("is_active", e.target.checked)}
            />
            启用模版(停用后成员端不可见,已生成的视频不受影响)
          </label>
          <div className="span-2 form-actions">
            <button type="button" onClick={onClose}>
              取消
            </button>
            <button className="primary" disabled={busy}>
              {busy ? "保存中..." : "保存模版"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function templateToForm(template: VideoTemplate): VideoTemplateForm {
  return {
    name: template.name,
    description: template.description,
    prompt: template.prompt,
    first_frame_prompt: template.first_frame_prompt,
    image_model: template.image_model,
    chat_model: template.chat_model,
    video_model: template.video_model,
    video_provider: template.video_provider,
    duration: template.duration,
    negative_prompt: template.negative_prompt,
    member_photo: template.member_photo,
    member_photo_hint: template.member_photo_hint,
    first_frame_confirm: template.first_frame_confirm,
    interaction_options: template.interaction_options,
    is_active: template.is_active,
    sort_order: template.sort_order,
    assets: template.assets.map(({ asset_id, usage, sort_order }) => ({
      asset_id,
      usage,
      sort_order,
    })),
  };
}

export default function CoCreationView({ profile }: { profile: OpsProfile }) {
  const canManage = ["owner", "admin"].includes(profile.membership?.role ?? "");
  const [templates, setTemplates] = useState<VideoTemplate[]>([]);
  const [videos, setVideos] = useState<CoCreationVideo[]>([]);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<{
    form: VideoTemplateForm;
    id: number | null;
  } | null>(null);

  const load = useCallback(async () => {
    try {
      const [templateRows, videoRows] = await Promise.all([
        listVideoTemplates(),
        canManage ? listCoCreationVideos() : Promise.resolve([]),
      ]);
      setTemplates(templateRows);
      setVideos(videoRows);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [canManage]);

  useEffect(() => {
    void load();
  }, [load]);

  // 有生成中的任务时定时刷新状态
  useEffect(() => {
    const hasActive = videos.some(
      (v) => v.status === "pending" || v.status === "generating_video",
    );
    if (!hasActive) return;
    const timer = setInterval(() => {
      void load();
    }, 10000);
    return () => clearInterval(timer);
  }, [videos, load]);

  const saveTemplate = async (form: VideoTemplateForm, id: number | null) => {
    if (id) await updateVideoTemplate(id, form);
    else await createVideoTemplate(form);
    await load();
  };

  return (
    <section>
      <div className="ops-section-head">
        <div>
          <h2>共创模版</h2>
          <p>
            模版 = 互动剧本 + IP 素材绑定:成员上传照片与企业 IP 形象合成同框首帧,
            确认后图生视频;企业锁住品牌要素,成员只填创意
          </p>
        </div>
        {canManage && (
          <button
            className="primary compact-button"
            onClick={() => setEditing({ form: EMPTY_FORM, id: null })}
          >
            新建模版
          </button>
        )}
      </div>
      {error && <div className="ops-alert error">{error}</div>}
      <div className="ops-table-wrap">
        <table className="ops-table">
          <thead>
            <tr>
              <th>模版</th>
              <th>出镜</th>
              <th>IP 素材</th>
              <th>视频模型</th>
              <th>时长</th>
              <th>状态</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {templates.map((template) => {
              const characterCount = template.assets.filter(
                (a) => a.usage === "character_reference",
              ).length;
              const promptTextCount = template.assets.filter(
                (a) => a.usage === "prompt_text",
              ).length;
              return (
                <tr key={template.id}>
                  <td>
                    <div className="template-cell">
                      {template.cover_url ? (
                        <img
                          className="template-thumb"
                          src={template.cover_url}
                          alt={template.name}
                        />
                      ) : (
                        <span className="template-thumb placeholder">🎬</span>
                      )}
                      <div>
                        <strong>{template.name}</strong>
                        {template.description && <small>{template.description}</small>}
                        {template.first_frame_confirm ? (
                          <small>首帧需成员确认</small>
                        ) : (
                          <small>首帧自动连出</small>
                        )}
                      </div>
                    </div>
                  </td>
                  <td>
                    <span
                      className={`ops-badge ${
                        template.member_photo === "required"
                          ? "status-active"
                          : template.member_photo === "optional"
                            ? "status-pending"
                            : "status-disabled"
                      }`}
                    >
                      {MEMBER_PHOTO_LABEL[template.member_photo]}
                    </span>
                  </td>
                  <td>
                    {characterCount > 0 ? `${characterCount} 张形象图` : "—"}
                    {promptTextCount > 0 && <small>{promptTextCount} 条特征文字</small>}
                  </td>
                  <td>
                    {template.video_model}
                    <small>
                      {template.video_provider === "openai_videos"
                        ? "Sora 风格"
                        : "任务式"}
                    </small>
                  </td>
                  <td>{template.duration}s</td>
                  <td>
                    <span
                      className={`ops-badge status-${template.is_active ? "active" : "disabled"}`}
                    >
                      {template.is_active ? "启用" : "停用"}
                    </span>
                  </td>
                  <td>
                    {canManage && (
                      <div className="row-actions">
                        <button
                          onClick={() =>
                            setEditing({ form: templateToForm(template), id: template.id })
                          }
                        >
                          编辑
                        </button>
                        <button
                          onClick={async () => {
                            try {
                              await updateVideoTemplate(template.id, {
                                is_active: !template.is_active,
                              });
                              await load();
                            } catch (err) {
                              setError(errorMessage(err));
                            }
                          }}
                        >
                          {template.is_active ? "停用" : "启用"}
                        </button>
                        <button
                          className="danger-text"
                          onClick={async () => {
                            if (
                              !window.confirm(
                                `确定删除模版「${template.name}」吗?已生成的视频记录会保留。`,
                              )
                            )
                              return;
                            try {
                              await deleteVideoTemplate(template.id);
                              await load();
                            } catch (err) {
                              setError(errorMessage(err));
                            }
                          }}
                        >
                          删除
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!templates.length && (
          <div className="empty-row">
            还没有模版。配置互动剧本并绑定 IP 素材后,企业成员在创作平台的「共创」tab
            里即可和你家 IP 合拍。
          </div>
        )}
      </div>

      {canManage && (
        <div className="admin-subsection">
          <div className="ops-section-head">
            <div>
              <h3>成员共创视频</h3>
              <p>企业全部成员基于模版生成的视频;生成完成后可在线查看</p>
            </div>
            <button onClick={() => void load()}>刷新</button>
          </div>
          <div className="ops-table-wrap">
            <table className="ops-table">
              <thead>
                <tr>
                  <th>成员</th>
                  <th>模版</th>
                  <th>创意</th>
                  <th>状态</th>
                  <th>提交时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {videos.map((video) => (
                  <tr key={video.id}>
                    <td>
                      <strong>{video.creator_name || video.creator_sub}</strong>
                      <small>{video.creator_sub}</small>
                    </td>
                    <td>{video.template_name || "—"}</td>
                    <td>
                      <span className="cocreation-idea">{video.input_text}</span>
                    </td>
                    <td>
                      <span
                        className={`ops-badge status-${
                          video.status === "completed"
                            ? "active"
                            : video.status === "failed"
                              ? "rejected"
                              : "pending"
                        }`}
                      >
                        {STATUS_LABEL[video.status] ?? video.status}
                      </span>
                      {video.error && <small>{video.error}</small>}
                    </td>
                    <td>{formatDateTime(video.created_at)}</td>
                    <td>
                      {video.video_url && (
                        <a
                          href={video.video_url}
                          target="_blank"
                          rel="noreferrer"
                          download
                        >
                          查看视频
                        </a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!videos.length && (
              <div className="empty-row">暂无成员共创视频</div>
            )}
          </div>
        </div>
      )}

      {editing && (
        <TemplateForm
          key={editing.id}
          initial={editing}
          onSave={saveTemplate}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}
