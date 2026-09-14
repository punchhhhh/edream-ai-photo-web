import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  ArrowLeft,
  Boxes,
  Building2,
  Check,
  Clapperboard,
  Copy,
  Download,
  ExternalLink,
  Link2,
  LogIn,
  LogOut,
  QrCode,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import QRCode from "qrcode";
import {
  addPlatformAdmin,
  applyEnterprise,
  cancelEnterprise,
  changeEnterpriseStatus,
  createEnterpriseEntry,
  deletePlatformAdmin,
  disableEnterpriseEntry,
  fetchOpsProfile,
  getEnterpriseEntry,
  getEnterpriseQuota,
  listEnterprises,
  listPlatformAdmins,
  logoutOps,
  reviewEnterprise,
  updateEnterprise,
  updateEnterpriseEntry,
  updateEnterpriseQuota,
} from "./api";
import type {
  Enterprise,
  EnterpriseEntry,
  EnterpriseForm,
  OpsProfile,
  PlatformAdmin,
  Quota,
} from "./types";
import MaterialsView from "./MaterialsView";
import CoCreationView from "./CoCreationView";
import {
  beginLogin,
  completeLogout,
  isExplicitlyLoggedOut,
} from "../authNavigation";
import "./ops.css";
const EMPTY_ENTERPRISE: EnterpriseForm = {
  name: "",
  credit_code: "",
  contact_name: "",
  contact_phone: "",
  contact_email: "",
  description: "",
};

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function formatDate(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败";
}

function StatusBadge({ status }: { status: string }) {
  const labels: Record<string, string> = {
    pending: "待审核",
    approved: "已认证",
    rejected: "已驳回",
    suspended: "已停用",
    archived: "已归档",
    active: "正常",
    disabled: "已停用",
  };
  return (
    <span className={`ops-badge status-${status}`}>
      {labels[status] ?? status}
    </span>
  );
}

function EnterpriseFormView({
  initial,
  submitLabel,
  onSubmit,
  busy,
}: {
  initial?: Enterprise | null;
  submitLabel: string;
  onSubmit: (form: EnterpriseForm) => Promise<void>;
  busy: boolean;
}) {
  const [form, setForm] = useState<EnterpriseForm>(() =>
    initial
      ? {
          name: initial.name,
          credit_code: initial.credit_code,
          contact_name: initial.contact_name,
          contact_phone: initial.contact_phone,
          contact_email: initial.contact_email,
          description: initial.description,
        }
      : EMPTY_ENTERPRISE,
  );
  const set = (key: keyof EnterpriseForm, value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));
  return (
    <form
      className="ops-form enterprise-form"
      onSubmit={(event) => {
        event.preventDefault();
        void onSubmit(form);
      }}
    >
      <label>
        企业名称
        <input
          required
          maxLength={200}
          value={form.name}
          onChange={(e) => set("name", e.target.value)}
        />
      </label>
      <label>
        统一社会信用代码
        <input
          required
          maxLength={100}
          value={form.credit_code}
          onChange={(e) => set("credit_code", e.target.value)}
        />
      </label>
      <label>
        联系人
        <input
          required
          maxLength={100}
          value={form.contact_name}
          onChange={(e) => set("contact_name", e.target.value)}
        />
      </label>
      <label>
        联系电话
        <input
          maxLength={50}
          value={form.contact_phone}
          onChange={(e) => set("contact_phone", e.target.value)}
        />
      </label>
      <label>
        联系邮箱
        <input
          type="email"
          maxLength={255}
          value={form.contact_email}
          onChange={(e) => set("contact_email", e.target.value)}
        />
      </label>
      <label className="span-2">
        企业说明
        <textarea
          rows={3}
          maxLength={1000}
          value={form.description}
          onChange={(e) => set("description", e.target.value)}
        />
      </label>
      <div className="span-2 form-actions">
        <button className="primary" disabled={busy}>
          {busy ? "提交中..." : submitLabel}
        </button>
      </div>
    </form>
  );
}

function CertificationView({
  profile,
  reload,
}: {
  profile: OpsProfile;
  reload: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (form: EnterpriseForm) => {
    setBusy(true);
    setError("");
    try {
      if (profile.enterprise) await updateEnterprise(form);
      else await applyEnterprise(form);
      await reload();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  if (!profile.enterprise)
    return (
      <main className="ops-centered">
        <div className="ops-page-title">
          <p>企业认证</p>
          <h1>完善企业主体信息</h1>
          <span>审核通过后启用企业素材库。</span>
        </div>
        {error && <div className="ops-alert error">{error}</div>}
        <EnterpriseFormView
          submitLabel="提交认证"
          onSubmit={submit}
          busy={busy}
        />
      </main>
    );
  const enterprise = profile.enterprise;
  if (enterprise.status === "suspended" || enterprise.status === "archived")
    return (
      <main className="ops-centered compact">
        <div className="ops-page-title">
          <p>企业状态</p>
          <h1>{enterprise.name}</h1>
        </div>
        <div className="ops-state-panel">
          <StatusBadge status={enterprise.status} />
          <p>
            {enterprise.status === "suspended"
              ? "企业服务已暂停，请联系平台管理员。"
              : "企业已归档。"}
          </p>
        </div>
      </main>
    );
  return (
    <main className="ops-centered">
      <div className="ops-page-title">
        <p>企业认证</p>
        <h1>{enterprise.name}</h1>
        <StatusBadge status={enterprise.status} />
      </div>
      {enterprise.status === "pending" && (
        <div className="ops-alert">
          资料正在审核，修改企业名称或信用代码后会重新进入审核。
        </div>
      )}
      {enterprise.status === "rejected" && (
        <div className="ops-alert error">
          驳回原因：{enterprise.rejection_reason}
        </div>
      )}
      {error && <div className="ops-alert error">{error}</div>}
      <EnterpriseFormView
        initial={enterprise}
        submitLabel={
          enterprise.status === "rejected" ? "修改并重新提交" : "更新资料"
        }
        onSubmit={submit}
        busy={busy}
      />
      {enterprise.status !== "approved" && (
        <button
          className="danger-text"
          onClick={async () => {
            if (!window.confirm("确定撤销本次企业认证吗？")) return;
            setBusy(true);
            try {
              await cancelEnterprise();
              window.location.reload();
            } catch (err) {
              setError(errorMessage(err));
              setBusy(false);
            }
          }}
        >
          撤销认证
        </button>
      )}
    </main>
  );
}

function EnterpriseEntryView() {
  const [entry, setEntry] = useState<EnterpriseEntry | null>(null);
  const [qrCode, setQrCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      setEntry(await getEnterpriseEntry());
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!entry?.active || !entry.entry_url) {
      setQrCode("");
      return;
    }
    void QRCode.toDataURL(entry.entry_url, {
      width: 320,
      margin: 2,
      errorCorrectionLevel: "M",
      color: { dark: "#164e42", light: "#ffffff" },
    })
      .then(setQrCode)
      .catch(() => setError("二维码生成失败，请刷新后重试"));
  }, [entry]);

  const generate = async () => {
    if (
      entry?.active &&
      !window.confirm("更新后原链接和二维码将立即失效，确定继续吗？")
    )
      return;
    setBusy(true);
    setError("");
    try {
      setEntry(await createEnterpriseEntry());
      setCopied(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleAutoJoin = async (autoJoin: boolean) => {
    if (
      autoJoin &&
      !window.confirm(
        "开启后，任何拿到链接的登录用户都会自动加入企业并使用共创额度，确定继续吗？"
      )
    )
      return;
    setBusy(true);
    setError("");
    try {
      setEntry(await updateEnterpriseEntry({ auto_join: autoJoin }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    if (!window.confirm("停用后当前链接和二维码将无法访问，确定继续吗？"))
      return;
    setBusy(true);
    setError("");
    try {
      setEntry(await disableEnterpriseEntry());
      setCopied(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="enterprise-entry-view">
      <div className="ops-section-head">
        <div>
          <h2>企业入口</h2>
          <p>为当前企业生成专属分享链接；可开启自动加入，链接访客免确认成为成员并使用共创</p>
        </div>
      </div>
      {error && <div className="ops-alert error">{error}</div>}
      <div className="enterprise-entry-content">
        <div className="enterprise-entry-details">
          <div className="entry-status-row">
            <span className={`entry-status ${entry?.active ? "active" : "inactive"}`}>
              {entry?.active ? "入口已启用" : "尚未生成入口"}
            </span>
            {entry?.updated_at && <small>更新于 {formatDate(entry.updated_at)}</small>}
          </div>

          {entry?.active && entry.entry_url ? (
            <>
              <label className="entry-url-label" htmlFor="enterprise-entry-url">
                企业专属访问链接
              </label>
              <div className="entry-url-row">
                <input id="enterprise-entry-url" readOnly value={entry.entry_url} />
                <button
                  className="icon-button"
                  title="复制链接"
                  aria-label="复制链接"
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(entry.entry_url!);
                      setCopied(true);
                      window.setTimeout(() => setCopied(false), 1800);
                    } catch {
                      setError("浏览器未允许复制，请手动选择链接");
                    }
                  }}
                >
                  {copied ? <Check size={17} /> : <Copy size={17} />}
                </button>
                <a
                  className="entry-open-button"
                  href={entry.entry_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <ExternalLink size={16} />
                  打开
                </a>
              </div>
              <div className="entry-auto-join">
                <label className="entry-auto-join-toggle">
                  <input
                    type="checkbox"
                    checked={entry.auto_join}
                    disabled={busy}
                    onChange={(e) => void toggleAutoJoin(e.target.checked)}
                  />
                  <span>
                    自动加入
                    <small>
                      开启后，通过链接访问的登录用户自动加入企业，无需二次确认，
                      即可使用共创;关闭后链接仅对 Owner 定位企业有效。
                    </small>
                  </span>
                </label>
              </div>
              <p className="entry-security-note">
                {entry.auto_join
                  ? "自动加入已开启：链接即成员入口，请只分发给目标社群；成员的共创次数与首帧合成仍受平台限额约束。"
                  : "链接用于识别企业；未开启自动加入时，访问账号必须是该企业已认证的 Owner。"}
              </p>
              <div className="entry-actions">
                <button onClick={() => void generate()} disabled={busy}>
                  <RefreshCw size={16} />
                  更新链接
                </button>
                <button
                  className="danger-text-button"
                  onClick={() => void disable()}
                  disabled={busy}
                >
                  停用入口
                </button>
              </div>
            </>
          ) : (
            <div className="entry-empty-state">
              <Link2 size={26} />
              <strong>生成企业专属业务入口</strong>
              <span>生成后可复制链接或下载二维码，提供给企业唯一账号使用。</span>
              <button className="primary" onClick={() => void generate()} disabled={busy}>
                <Link2 size={16} />
                {busy ? "生成中..." : "生成入口"}
              </button>
            </div>
          )}
        </div>

        <div className="enterprise-entry-qr">
          <div className="entry-qr-title">
            <QrCode size={18} />
            <strong>入口二维码</strong>
          </div>
          {qrCode ? (
            <>
              <img src={qrCode} alt={`${entry?.enterprise_name ?? "企业"}专属入口二维码`} />
              <a href={qrCode} download="enterprise-entry.png">
                <Download size={16} />
                下载二维码
              </a>
            </>
          ) : (
            <div className="entry-qr-placeholder">
              <QrCode size={36} />
              <span>生成入口后显示</span>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function EnterpriseSettingsView({
  profile,
  reload,
}: {
  profile: OpsProfile;
  reload: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <section>
      <div className="ops-section-head">
        <div>
          <h2>企业资料</h2>
          <p>修改法定名称或信用代码后需要重新审核</p>
        </div>
        <StatusBadge status={profile.enterprise!.status} />
      </div>
      {error && <div className="ops-alert error">{error}</div>}
      <EnterpriseFormView
        initial={profile.enterprise}
        submitLabel="保存企业资料"
        busy={busy}
        onSubmit={async (form) => {
          setBusy(true);
          try {
            await updateEnterprise(form);
            await reload();
          } catch (err) {
            setError(errorMessage(err));
          } finally {
            setBusy(false);
          }
        }}
      />
    </section>
  );
}

function AdminView() {
  const [enterprises, setEnterprises] = useState<Enterprise[]>([]);
  const [admins, setAdmins] = useState<PlatformAdmin[]>([]);
  const [filter, setFilter] = useState("pending");
  const [newAdmin, setNewAdmin] = useState("");
  const [quotaEdit, setQuotaEdit] = useState<{
    enterprise: Enterprise;
    quota: Quota;
    gb: string;
  } | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      const [items, adminItems] = await Promise.all([
        listEnterprises(filter),
        listPlatformAdmins(),
      ]);
      setEnterprises(items);
      setAdmins(adminItems);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [filter]);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <section>
      <div className="ops-section-head">
        <div>
          <h2>平台管理</h2>
          <p>企业认证、状态与空间额度</p>
        </div>
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="pending">待审核</option>
          <option value="all">全部企业</option>
          <option value="approved">已认证</option>
          <option value="suspended">已停用</option>
        </select>
      </div>
      {error && <div className="ops-alert error">{error}</div>}
      <div className="ops-table-wrap">
        <table className="ops-table">
          <thead>
            <tr>
              <th>企业</th>
              <th>信用代码</th>
              <th>联系人</th>
              <th>状态</th>
              <th>提交时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {enterprises.map((enterprise) => (
              <tr key={enterprise.id}>
                <td>
                  <strong>{enterprise.name}</strong>
                  <small>{enterprise.contact_email}</small>
                </td>
                <td>{enterprise.credit_code}</td>
                <td>
                  {enterprise.contact_name}
                  <small>{enterprise.contact_phone}</small>
                </td>
                <td>
                  <StatusBadge status={enterprise.status} />
                </td>
                <td>{formatDate(enterprise.created_at)}</td>
                <td>
                  <div className="row-actions">
                    {enterprise.status === "pending" && (
                      <button
                        className="primary compact-button"
                        onClick={async () => {
                          try {
                            await reviewEnterprise(enterprise.id, "approved");
                            await load();
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        通过
                      </button>
                    )}
                    {enterprise.status === "pending" && (
                      <button
                        onClick={async () => {
                          const reason = window.prompt("请输入驳回原因");
                          if (!reason) return;
                          try {
                            await reviewEnterprise(
                              enterprise.id,
                              "rejected",
                              reason,
                            );
                            await load();
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        驳回
                      </button>
                    )}
                    {enterprise.status === "approved" && (
                      <button
                        onClick={async () => {
                          try {
                            await changeEnterpriseStatus(
                              enterprise.id,
                              "suspended",
                              "平台管理员停用",
                            );
                            await load();
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        停用
                      </button>
                    )}
                    {enterprise.status === "suspended" && (
                      <button
                        onClick={async () => {
                          try {
                            await changeEnterpriseStatus(
                              enterprise.id,
                              "approved",
                            );
                            await load();
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        恢复
                      </button>
                    )}
                    {["approved", "suspended"].includes(enterprise.status) && (
                      <button
                        onClick={async () => {
                          try {
                            const quota = await getEnterpriseQuota(
                              enterprise.id,
                            );
                            setQuotaEdit({
                              enterprise,
                              quota,
                              gb: (quota.limit_bytes / 1024 ** 3).toFixed(2),
                            });
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        额度
                      </button>
                    )}
                    {["approved", "suspended"].includes(enterprise.status) && (
                      <button
                        className="danger-text"
                        onClick={async () => {
                          if (
                            !window.confirm(
                              "归档后企业成员和素材将不可用，确定继续吗？",
                            )
                          ) {
                            return;
                          }
                          try {
                            await changeEnterpriseStatus(
                              enterprise.id,
                              "archived",
                              "平台管理员归档",
                            );
                            await load();
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        归档
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!enterprises.length && (
          <div className="empty-row">没有符合条件的企业</div>
        )}
      </div>
      <div className="admin-subsection">
        <div className="ops-section-head">
          <div>
            <h3>平台管理员</h3>
            <p>环境变量账号用于引导，数据库角色可在此维护</p>
          </div>
        </div>
        <form
          className="member-add"
          onSubmit={async (event) => {
            event.preventDefault();
            try {
              await addPlatformAdmin(newAdmin);
              setNewAdmin("");
              await load();
            } catch (err) {
              setError(errorMessage(err));
            }
          }}
        >
          <label>
            Casdoor 用户标识
            <input
              required
              value={newAdmin}
              onChange={(e) => setNewAdmin(e.target.value)}
            />
          </label>
          <button className="primary">添加管理员</button>
        </form>
        <div className="admin-list">
          {admins.map((admin) => (
            <div key={`${admin.source}-${admin.oauth_sub}`}>
              <span>
                <strong>{admin.display_name || admin.oauth_sub}</strong>
                <small>
                  {admin.source === "environment" ? "环境变量" : "平台角色"}
                </small>
              </span>
              {admin.source === "database" && (
                <button
                  className="danger-text"
                  onClick={async () => {
                    if (!window.confirm("确定移除该平台管理员吗？")) return;
                    try {
                      await deletePlatformAdmin(admin.user_id);
                      await load();
                    } catch (err) {
                      setError(errorMessage(err));
                    }
                  }}
                >
                  移除
                </button>
              )}
            </div>
          ))}
        </div>
      </div>
      {quotaEdit && (
        <div
          className="ops-modal-backdrop"
          onMouseDown={() => setQuotaEdit(null)}
        >
          <div
            className="ops-modal small"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="modal-head">
              <h3>{quotaEdit.enterprise.name} · 空间额度</h3>
              <button aria-label="关闭" onClick={() => setQuotaEdit(null)}>
                ×
              </button>
            </div>
            <p className="modal-note">
              当前使用 {formatBytes(quotaEdit.quota.used_bytes)}
            </p>
            <form
              className="ops-form"
              onSubmit={async (event) => {
                event.preventDefault();
                try {
                  await updateEnterpriseQuota(
                    quotaEdit.enterprise.id,
                    Number(quotaEdit.gb) * 1024 ** 3,
                  );
                  setQuotaEdit(null);
                  await load();
                } catch (err) {
                  setError(errorMessage(err));
                }
              }}
            >
              <label className="span-2">
                总额度（GB）
                <input
                  required
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={quotaEdit.gb}
                  onChange={(e) =>
                    setQuotaEdit({ ...quotaEdit, gb: e.target.value })
                  }
                />
              </label>
              <div className="span-2 form-actions">
                <button type="button" onClick={() => setQuotaEdit(null)}>
                  取消
                </button>
                <button className="primary">保存额度</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </section>
  );
}

export default function OpsApp() {
  const [profile, setProfile] = useState<OpsProfile | null>(null);
  const [loggedOut, setLoggedOut] = useState(isExplicitlyLoggedOut);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<
    "materials" | "entry" | "enterprise" | "cocreation" | "admin"
  >("materials");
  const reload = useCallback(async () => {
    if (loggedOut) {
      setLoading(false);
      return;
    }
    try {
      setProfile(await fetchOpsProfile());
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [loggedOut]);
  useEffect(() => {
    document.title = "eDream Ops";
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  const enterpriseReady =
    profile?.enterprise?.status === "approved" &&
    profile.membership?.status === "active";
  const visibleTab = useMemo(
    () => (!enterpriseReady && profile?.is_platform_admin ? "admin" : tab),
    [enterpriseReady, profile, tab],
  );
  if (loggedOut)
    return (
      <div className="ops-shell">
        <header className="ops-header">
          <a className="ops-brand" href="/ops">
            <span>eD</span>
            <div>
              <strong>eDream 企业中心</strong>
              <small>素材与企业入口</small>
            </div>
          </a>
        </header>
        <main className="ops-centered compact">
          <div className="ops-page-title">
            <p>账号状态</p>
            <h1>已退出登录</h1>
            <span>当前页面已保留，重新登录后可继续使用。</span>
          </div>
          <button className="primary-button" onClick={() => beginLogin("/ops")}>
            <LogIn size={17} />
            重新登录
          </button>
        </main>
      </div>
    );
  if (loading) return <div className="ops-loading">正在加载运营平台...</div>;
  if (!profile)
    return (
      <div className="ops-loading error">{error || "无法加载运营平台"}</div>
    );
  return (
    <div className="ops-shell">
      <header className="ops-header">
        <a className="ops-brand" href="/ops">
          <span>eD</span>
          <div>
            <strong>eDream 企业中心</strong>
            <small>素材与企业入口</small>
          </div>
        </a>
        <div className="ops-header-meta">
          {profile.enterprise && (
            <span className="tenant-chip">{profile.enterprise.name}</span>
          )}
          <span className="current-user">
            {profile.user.display_name || profile.user.oauth_sub}
          </span>
          <button
            className="icon-button"
            aria-label="退出登录"
            title="退出登录"
            onClick={async () => {
              let ssoLogoutUrl: string | null = null;
              try {
                ssoLogoutUrl = (await logoutOps()).sso_logout_url;
              } catch {
                /* The local session may already be expired. */
              }
              setLoggedOut(true);
              setProfile(null);
              completeLogout(ssoLogoutUrl);
            }}
          >
            <LogOut size={17} />
          </button>
        </div>
      </header>
      {enterpriseReady || profile.is_platform_admin ? (
        <div className="ops-workspace">
          <aside className="ops-sidebar">
            {enterpriseReady && (
              <div className="ops-nav-group">
                <small>企业工作台</small>
                <button
                  className={visibleTab === "materials" ? "active" : ""}
                  onClick={() => setTab("materials")}
                >
                  <Boxes size={17} />
                  素材管理
                </button>
                <button
                  className={visibleTab === "cocreation" ? "active" : ""}
                  onClick={() => setTab("cocreation")}
                >
                  <Clapperboard size={17} />
                  视频共创
                </button>
                <button
                  className={visibleTab === "entry" ? "active" : ""}
                  onClick={() => setTab("entry")}
                >
                  <Link2 size={17} />
                  企业入口
                </button>
                <button
                  className={visibleTab === "enterprise" ? "active" : ""}
                  onClick={() => setTab("enterprise")}
                >
                  <Building2 size={17} />
                  企业资料
                </button>
              </div>
            )}
            {profile.is_platform_admin && (
              <div className="ops-nav-group admin-nav-group">
                <small>平台运营</small>
                <button
                  className={visibleTab === "admin" ? "active" : ""}
                  onClick={() => setTab("admin")}
                >
                  <ShieldCheck size={17} />
                  企业审核
                </button>
              </div>
            )}
            <a className="back-to-studio" href="/" title="返回创作平台">
              <ArrowLeft size={16} />
              返回创作平台
            </a>
          </aside>
          <main className="ops-content">
            {visibleTab === "materials" && enterpriseReady && (
              <MaterialsView profile={profile} />
            )}
            {visibleTab === "cocreation" && enterpriseReady && (
              <CoCreationView profile={profile} />
            )}
            {visibleTab === "entry" && enterpriseReady && (
              <EnterpriseEntryView />
            )}
            {visibleTab === "enterprise" && enterpriseReady && (
              <EnterpriseSettingsView profile={profile} reload={reload} />
            )}
            {visibleTab === "admin" && profile.is_platform_admin && (
              <AdminView />
            )}
          </main>
        </div>
      ) : (
        <CertificationView profile={profile} reload={reload} />
      )}
    </div>
  );
}
