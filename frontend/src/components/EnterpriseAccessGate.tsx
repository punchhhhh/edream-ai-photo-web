import { Clock3, LogIn, LogOut, ShieldCheck, Video } from 'lucide-react'
import { useState } from 'react'
import type {
  AuthUser,
  EnterpriseConsumerGrant,
  EnterpriseEntryPreview,
} from '../types'

interface Props {
  preview: EnterpriseEntryPreview | null
  grant: EnterpriseConsumerGrant | null
  user: AuthUser | null
  loading: boolean
  busy: boolean
  error: string
  onLogin: () => void
  onLogout: () => void
  onApply: () => Promise<void>
}

const STATUS_TEXT: Record<string, string> = {
  pending: '申请正在等待企业确认',
  expired: '上一次授权已到期，可以重新申请',
  exhausted: '上一次授权的视频次数已用完，可以重新申请',
  revoked: '上一次授权已被企业撤销，可以重新申请',
  rejected: '上一次申请未通过，可以重新申请',
}

export default function EnterpriseAccessGate({
  preview,
  grant,
  user,
  loading,
  busy,
  error,
  onLogin,
  onLogout,
  onApply,
}: Props) {
  const [accepted, setAccepted] = useState(false)

  return (
    <div className="entry-access-shell">
      <header className="entry-access-header">
        <a href="/" className="entry-access-brand">
          <span>eD</span>
          <strong>eDream 企业共创</strong>
        </a>
        {user && (
          <button className="btn" type="button" onClick={onLogout}>
            <LogOut size={16} />
            退出
          </button>
        )}
      </header>

      <main className="entry-access-main">
        {loading && <div className="callout">正在读取企业共创信息...</div>}
        {!loading && error && !preview && <div className="alert error">{error}</div>}
        {preview && !loading && (
          <>
            <div className="entry-access-title">
              <span>企业专属共创</span>
              <h1>{preview.enterprise_name}</h1>
              <p>{preview.enterprise_description || '使用企业提供的模版、模型配置和品牌素材生成视频。'}</p>
            </div>

            <section className="entry-access-details" aria-label="授权范围">
              <div>
                <Clock3 size={19} />
                <strong>{preview.grant_ttl_hours} 小时</strong>
                <span>授权有效期</span>
              </div>
              <div>
                <Video size={19} />
                <strong>{preview.video_limit} 个视频</strong>
                <span>本次可提交</span>
              </div>
              <div>
                <ShieldCheck size={19} />
                <strong>企业素材隔离</strong>
                <span>仅本次共创使用</span>
              </div>
            </section>

            {error && <div className="alert error">{error}</div>}
            {!user ? (
              <section className="entry-access-action">
                <h2>登录后开始共创</h2>
                <p>使用 Casdoor 账号登录；没有账号可在登录页直接注册。</p>
                <button className="btn primary big" type="button" onClick={onLogin}>
                  <LogIn size={18} />
                  登录或注册
                </button>
              </section>
            ) : grant?.status === 'pending' ? (
              <section className="entry-access-action">
                <h2>等待企业确认</h2>
                <p>申请通过后，刷新当前页面即可进入共创。</p>
              </section>
            ) : (
              <section className="entry-access-action">
                <h2>{grant ? STATUS_TEXT[grant.status] : '确认本次共创授权'}</h2>
                <label className="entry-consent">
                  <input
                    type="checkbox"
                    checked={accepted}
                    onChange={(event) => setAccepted(event.target.checked)}
                  />
                  <span>
                    我已阅读并同意共创服务与隐私说明，知悉企业可查看本次提交内容和生成视频。
                  </span>
                </label>
                <details className="entry-policy-details">
                  <summary>查看服务与隐私说明</summary>
                  <p>
                    本次共创会使用该企业配置的模型及专属素材。企业可以查看你的共创文字、任务状态和生成结果；
                    相关数据仅用于本次内容生成、运营审核和安全追溯，不会向其他企业开放。
                  </p>
                </details>
                <button
                  className="btn primary big"
                  type="button"
                  disabled={!accepted || busy}
                  onClick={() => void onApply()}
                >
                  <ShieldCheck size={18} />
                  {busy ? '正在申请...' : grant ? '重新申请共创' : '同意并开始共创'}
                </button>
              </section>
            )}
          </>
        )}
      </main>
    </div>
  )
}
