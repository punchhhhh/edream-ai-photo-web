import { useCallback, useEffect, useState } from "react";
import {
  createVideoTemplate,
  deleteVideoTemplate,
  listCoCreationVideos,
  listVideoTemplates,
  updateVideoTemplate,
} from "./api";
import type {
  CoCreationVideo,
  OpsProfile,
  VideoTemplate,
  VideoTemplateForm,
} from "./types";

const EMPTY_FORM: VideoTemplateForm = {
  name: "",
  description: "",
  prompt: "",
  chat_model: "",
  video_model: "",
  video_provider: "video_generations",
  duration: 5,
  negative_prompt: "",
  is_active: true,
  sort_order: 0,
};

const STATUS_LABEL: Record<string, string> = {
  pending: "排队中",
  generating_video: "生成中",
  completed: "已完成",
  failed: "失败",
};

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败";
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
  const set = <K extends keyof VideoTemplateForm>(key: K, value: VideoTemplateForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));
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
        <form
          className="ops-form"
          onSubmit={async (event) => {
            event.preventDefault();
            setBusy(true);
            setError("");
            try {
              await onSave(form, initial.id);
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
              placeholder="如:品牌宣传片"
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
              placeholder="一句话说明这个模版的用途"
            />
          </label>
          <label className="span-2">
            画面要求(生成时自动拼在成员创意前面)
            <textarea
              rows={3}
              maxLength={4000}
              value={form.prompt}
              onChange={(e) => set("prompt", e.target.value)}
              placeholder="如:画面需出现品牌 Logo,暖色调,镜头缓慢推进"
            />
          </label>
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
            成员基于模版用企业网关密钥生成视频,每人可保留的视频数有上限
            (由服务端 COCREATION_MAX_VIDEOS_PER_USER 配置,默认 3);
            网关密钥按企业 Owner 的 Casdoor 标识自动获取
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
              <th>视频模型</th>
              <th>时长</th>
              <th>状态</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {templates.map((template) => (
              <tr key={template.id}>
                <td>
                  <strong>{template.name}</strong>
                  {template.description && <small>{template.description}</small>}
                  {template.chat_model && (
                    <small>文本模型:{template.chat_model}</small>
                  )}
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
                  <span className={`ops-badge status-${template.is_active ? "active" : "disabled"}`}>
                    {template.is_active ? "启用" : "停用"}
                  </span>
                </td>
                <td>
                  {canManage && (
                    <div className="row-actions">
                      <button
                        onClick={() =>
                          setEditing({
                            form: {
                              name: template.name,
                              description: template.description,
                              prompt: template.prompt,
                              chat_model: template.chat_model,
                              video_model: template.video_model,
                              video_provider: template.video_provider,
                              duration: template.duration,
                              negative_prompt: template.negative_prompt,
                              is_active: template.is_active,
                              sort_order: template.sort_order,
                            },
                            id: template.id,
                          })
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
            ))}
          </tbody>
        </table>
        {!templates.length && (
          <div className="empty-row">
            还没有模版。配置模版后,企业成员在创作平台的「共创」tab 里即可使用。
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
          initial={editing}
          onSave={saveTemplate}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}
