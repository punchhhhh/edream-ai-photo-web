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
  LogOut,
  ShieldCheck,
  Users,
} from "lucide-react";
import {
  addMember,
  addPlatformAdmin,
  applyEnterprise,
  cancelEnterprise,
  changeEnterpriseStatus,
  deleteMember,
  deletePlatformAdmin,
  fetchOpsProfile,
  getEnterpriseQuota,
  listEnterprises,
  listMembers,
  listPlatformAdmins,
  reviewEnterprise,
  updateEnterprise,
  updateEnterpriseQuota,
  updateMember,
} from "./api";
import type {
  Enterprise,
  EnterpriseForm,
  EnterpriseMembership,
  MemberRole,
  OpsProfile,
  PlatformAdmin,
  Quota,
} from "./types";
import MaterialsView from "./MaterialsView";
import "./ops.css";
const ROLE_LABEL: Record<MemberRole, string> = {
  owner: "Owner",
  admin: "管理员",
  editor: "编辑",
  viewer: "只读",
};
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

function MembersView({ profile }: { profile: OpsProfile }) {
  const [members, setMembers] = useState<EnterpriseMembership[]>([]);
  const [sub, setSub] = useState("");
  const [role, setRole] = useState<Exclude<MemberRole, "owner">>("viewer");
  const [error, setError] = useState("");
  const canManage = ["owner", "admin"].includes(profile.membership?.role ?? "");
  const load = useCallback(async () => {
    try {
      setMembers(await listMembers());
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <section>
      <div className="ops-section-head">
        <div>
          <h2>企业成员</h2>
          <p>
            {members.filter((item) => item.status === "active").length}{" "}
            个启用账号
          </p>
        </div>
      </div>
      {error && <div className="ops-alert error">{error}</div>}
      {canManage && (
        <form
          className="member-add"
          onSubmit={async (event) => {
            event.preventDefault();
            try {
              await addMember(sub, role);
              setSub("");
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
              value={sub}
              onChange={(e) => setSub(e.target.value)}
              placeholder="oauth_sub"
            />
          </label>
          <label>
            角色
            <select
              value={role}
              onChange={(e) =>
                setRole(e.target.value as Exclude<MemberRole, "owner">)
              }
            >
              <option value="admin">管理员</option>
              <option value="editor">编辑</option>
              <option value="viewer">只读</option>
            </select>
          </label>
          <button className="primary">添加成员</button>
        </form>
      )}
      <div className="ops-table-wrap">
        <table className="ops-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>角色</th>
              <th>状态</th>
              <th>加入时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.id}>
                <td>
                  <strong>{member.display_name || member.oauth_sub}</strong>
                  <small>{member.email || member.oauth_sub}</small>
                </td>
                <td>
                  {canManage && member.role !== "owner" ? (
                    <select
                      value={member.role}
                      onChange={async (e) => {
                        try {
                          await updateMember(member.id, {
                            role: e.target.value as MemberRole,
                          });
                          await load();
                        } catch (err) {
                          setError(errorMessage(err));
                        }
                      }}
                    >
                      {["admin", "editor", "viewer"].map((value) => (
                        <option key={value} value={value}>
                          {ROLE_LABEL[value as MemberRole]}
                        </option>
                      ))}
                    </select>
                  ) : (
                    ROLE_LABEL[member.role]
                  )}
                </td>
                <td>
                  <StatusBadge status={member.status} />
                </td>
                <td>{formatDate(member.created_at)}</td>
                <td>
                  {canManage && member.role !== "owner" && (
                    <div className="row-actions">
                      <button
                        onClick={async () => {
                          try {
                            await updateMember(member.id, {
                              status:
                                member.status === "active"
                                  ? "disabled"
                                  : "active",
                            });
                            await load();
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        {member.status === "active" ? "停用" : "启用"}
                      </button>
                      <button
                        className="danger-text"
                        onClick={async () => {
                          if (!window.confirm("确定移除该成员吗？")) return;
                          try {
                            await deleteMember(member.id);
                            await load();
                          } catch (err) {
                            setError(errorMessage(err));
                          }
                        }}
                      >
                        移除
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<
    "materials" | "members" | "enterprise" | "admin"
  >("materials");
  const reload = useCallback(async () => {
    try {
      setProfile(await fetchOpsProfile());
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);
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
            <small>素材与账号管理</small>
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
              const response = await fetch("/api/auth/logout", {
                method: "POST",
              });
              const data = await response.json();
              window.location.href = data.sso_logout_url || "/";
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
                  className={visibleTab === "members" ? "active" : ""}
                  onClick={() => setTab("members")}
                >
                  <Users size={17} />
                  企业成员
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
            {visibleTab === "members" && enterpriseReady && (
              <MembersView profile={profile} />
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
