import { type FormEvent, useEffect, useState } from 'react'
import { errorText, session, signOut } from './api'
import CampaignCreator from './CampaignCreator'
import HelpHint, { FieldHelp } from './HelpHint'
import StreamEditor from './StreamEditor'

type Section = 'create' | 'editor'

function Brand() {
  return (
    <div className="brand">
      <span className="brand__mark" aria-hidden="true">A</span>
      <div><strong>Adrobot</strong><small>Keitaro Workspace</small></div>
    </div>
  )
}

function Login({ onLogin }: { onLogin: (username: string) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const data = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    try {
      const user = await session({
        username: String(data.get('username') ?? ''),
        password: String(data.get('password') ?? ''),
      })
      if (user.username) onLogin(user.username)
    } catch (requestError) {
      setError(errorText(requestError))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="entry">
      <form className="panel form-stack" onSubmit={submit}>
        <Brand />
        <h1>Вход в рабочее пространство</h1>
        <div className="field">
          <FieldHelp id="login-username" label="Имя пользователя" text="Введите имя своей учётной записи Adrobot." />
          <input id="login-username" name="username" autoComplete="username" required disabled={busy} />
        </div>
        <div className="field">
          <FieldHelp id="login-password" label="Пароль" text="Введите пароль той же учётной записи Adrobot." />
          <input id="login-password" name="password" type="password" autoComplete="current-password" required disabled={busy} />
        </div>
        {error && <p className="notice notice--error" role="alert">{error}</p>}
        <HelpHint label="Войти" text="После входа откроются создание кампаний и редактор потоков."
          control={<button className="button button--primary" disabled={busy}>{busy ? 'Входим…' : 'Войти'}</button>} />
      </form>
    </main>
  )
}

function initialCampaign(): number | null {
  const value = Number(new URLSearchParams(location.search).get('campaign'))
  return Number.isSafeInteger(value) && value > 0 ? value : null
}

export default function App() {
  const [username, setUsername] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  const [signingOut, setSigningOut] = useState(false)
  const [campaignId, setCampaignId] = useState(initialCampaign)
  const [section, setSection] = useState<Section>(() => (
    new URLSearchParams(location.search).get('section') === 'editor' ? 'editor' : 'create'
  ))

  function loadSession() {
    setReady(false)
    setError('')
    session().then((view) => setUsername(view.username)).catch((requestError: unknown) => {
      setError(errorText(requestError))
    }).finally(() => setReady(true))
  }

  useEffect(loadSession, [])

  useEffect(() => {
    const url = new URL(location.href)
    url.searchParams.set('section', section)
    if (campaignId === null) url.searchParams.delete('campaign')
    else url.searchParams.set('campaign', String(campaignId))
    history.replaceState(null, '', url)
  }, [section, campaignId])

  async function logout() {
    setSigningOut(true)
    setError('')
    try {
      await signOut()
      setUsername(null)
    } catch (requestError) {
      setError(errorText(requestError))
    } finally {
      setSigningOut(false)
    }
  }

  if (!ready) return <main className="entry" role="status">Проверяем сессию…</main>
  if (!username && error) return (
    <main className="entry">
      <div className="panel form-stack">
        <p className="notice notice--error" role="alert">{error}</p>
        <HelpHint label="Повторить" text="Заново проверит, открыта ли ваша сессия."
          control={<button className="button button--subtle" onClick={loadSession}>Повторить</button>} />
      </div>
    </main>
  )
  if (!username) return <Login onLogin={setUsername} />

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Brand />
        <nav aria-label="Рабочее пространство">
          <button aria-current={section === 'create' ? 'page' : undefined}
            onClick={() => setSection('create')}>Создать кампанию</button>
          <button aria-current={section === 'editor' ? 'page' : undefined}
            onClick={() => setSection('editor')}>Редактор потоков</button>
        </nav>
      </aside>
      <main className="content">
        <header className="topbar">
          <span>{section === 'create' ? 'Кампании' : 'Кампании / Потоки'}</span>
          <div><span>{username}</span>
            <HelpHint label="Выйти" text="Закроет вашу сессию. Сохранённые черновики можно открыть после следующего входа."
              control={<button className="button button--subtle" disabled={signingOut} onClick={logout}>Выйти</button>} />
          </div>
        </header>
        <div className="page">
          {error && <p className="notice notice--error" role="alert">{error}</p>}
          {section === 'create' ? (
            <CampaignCreator username={username} openEditor={(id) => {
              setCampaignId(id)
              setSection('editor')
            }} />
          ) : <StreamEditor campaignId={campaignId} onCampaign={setCampaignId} />}
        </div>
      </main>
    </div>
  )
}
