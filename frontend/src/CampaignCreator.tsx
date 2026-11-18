import { type FormEvent, useEffect, useState } from 'react'
import {
  type CampaignInput, type CampaignResult, type Catalog, errorText, type Item, workspace,
} from './api'
import HelpHint, { FieldHelp } from './HelpHint'
import OfferPicker from './OfferPicker'

type FormFields = {
  name: string
  alias: string
  geo: string
  domain_id: string
  group_id: string
  traffic_source_id: string
  offer: Item | null
}

function initialForm(storageKey: string): FormFields {
  const defaults = { name: '', alias: '', geo: '', domain_id: '', group_id: '', traffic_source_id: '', offer: null }
  try {
    return { ...defaults, ...JSON.parse(sessionStorage.getItem(storageKey) ?? '{}') }
  } catch {
    return defaults
  }
}

export default function CampaignCreator({ username, openEditor }: {
  username: string
  openEditor: (id: number) => void
}) {
  const storageKey = `adrobot:create:${username}`
  const [form, setForm] = useState(() => initialForm(storageKey))
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [result, setResult] = useState<CampaignResult | null>(null)
  const [error, setError] = useState('')
  const [catalogError, setCatalogError] = useState('')
  const [loadingCatalog, setLoadingCatalog] = useState(true)
  const [busy, setBusy] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    sessionStorage.setItem(storageKey, JSON.stringify(form))
  }, [form, storageKey])

  useEffect(() => {
    const controller = new AbortController()
    setLoadingCatalog(true)
    workspace<Catalog>('catalog', undefined, controller.signal)
      .then((data) => {
        setCatalog(data)
        setCatalogError('')
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setCatalogError(errorText(requestError))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingCatalog(false)
      })
    return () => controller.abort()
  }, [attempt])

  function change<K extends keyof FormFields>(key: K, value: FormFields[K]) {
    setForm((previous) => ({ ...previous, [key]: value }))
    setResult(null)
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy || !form.offer) return
    const alias = form.alias || `campaign-${form.geo.toLowerCase()}-${crypto.randomUUID().slice(0, 8)}`
    const payload: CampaignInput = {
      name: form.name.trim(), geo: form.geo.toUpperCase(), alias,
      offer_id: form.offer.id, domain_id: Number(form.domain_id),
      group_id: Number(form.group_id), traffic_source_id: Number(form.traffic_source_id),
    }
    setForm((previous) => ({ ...previous, alias }))
    sessionStorage.setItem(storageKey, JSON.stringify({ ...form, alias }))
    setBusy(true)
    setError('')
    try {
      setResult(await workspace<CampaignResult>('campaigns', payload))
    } catch (requestError) {
      setError(errorText(requestError))
    } finally {
      setBusy(false)
    }
  }

  const references = [
    { key: 'domain_id', label: 'Домен', options: catalog?.domains, help: 'Выберите домен, на котором будет открываться ссылка на кампанию. В списке активные домены из Keitaro.' },
    { key: 'group_id', label: 'Группа', options: catalog?.groups, help: 'Выберите, в какой группе кампаний Keitaro должна появиться новая кампания.' },
    { key: 'traffic_source_id', label: 'Источник трафика', options: catalog?.sources, help: 'Выберите источник, откуда придут посетители. Keitaro использует его настройки для учёта трафика.' },
  ] as const

  return (
    <>
      <header className="page-heading">
        <h1>Создание кампании</h1>
        <p>Посетители из выбранной страны попадут на Google, остальные — на выбранный оффер.</p>
      </header>
      <div className="creator-grid">
        <form className="panel form-stack" onSubmit={submit}>
          <h2>Настройки Keitaro</h2>
          <fieldset disabled={busy}>
            <div className="field">
              <FieldHelp id="campaign-name" label="Название кампании"
                text="Введите имя, по которому вам будет легко найти кампанию в Keitaro." />
              <input id="campaign-name" required maxLength={255} value={form.name} onChange={(event) => change('name', event.target.value)}
                placeholder="Например, Кампания для Австралии" />
            </div>
            <div className="form-row">
              <div className="field">
                <FieldHelp id="campaign-geo" label="Код страны"
                  text="Введите две латинские буквы, например AU для Австралии. Посетители из этой страны попадут на Google." />
                <input id="campaign-geo" required pattern="[A-Za-z]{2}" maxLength={2}
                  value={form.geo} onChange={(event) => change('geo', event.target.value.toUpperCase())}
                  placeholder="AU" autoCapitalize="characters" />
              </div>
              <div className="field">
                <FieldHelp id="campaign-alias" label="Алиас"
                  text="Это короткое имя в ссылке на кампанию. Можно оставить поле пустым. Если задаёте своё, используйте строчные латинские буквы, цифры, дефис или подчёркивание." />
                <input id="campaign-alias" value={form.alias} onChange={(event) => change('alias', event.target.value)}
                  maxLength={64} pattern="[a-z0-9][a-z0-9_\-]{2,63}" placeholder="Создадим автоматически" />
              </div>
            </div>
            {references.map(({ key, label, options, help }) => (
              <div className="field" key={key}>
                <FieldHelp id={key} label={label} text={help} />
                <select id={key} required value={form[key]} disabled={!options?.length}
                  onChange={(event) => change(key, event.target.value)}>
                  <option value="">{loadingCatalog ? 'Загрузка…' : options?.length ? 'Выберите значение' : 'Нет доступных значений'}</option>
                  {options?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
              </div>
            ))}
            <OfferPicker value={form.offer} onPick={(offer) => change('offer', offer)}
              help="Введите название или ID и выберите оффер. На него пойдут посетители из остальных стран." />
          </fieldset>
          {catalogError && (
            <div className="notice notice--error" role="alert">
              {catalogError}
              <HelpHint label="Обновить справочники" text="Заново загрузит домены, группы и источники трафика из Keitaro."
                control={<button className="button button--subtle" type="button" onClick={() => setAttempt(attempt + 1)}>
                  Обновить справочники
                </button>} />
            </div>
          )}
          {error && <p className="notice notice--error" role="alert">{error}</p>}
          <div className="panel-actions">
            <HelpHint label={result?.status === 'UNCERTAIN' ? 'Сверить результат' : 'Создать кампанию'}
              text={result?.status === 'UNCERTAIN'
                ? 'Проверит по алиасу, успела ли кампания создаться в Keitaro.'
                : 'Эта кнопка создаёт кампанию в Keitaro и включает её после проверки обоих потоков.'}
              control={<button className="button button--primary" type="submit"
                disabled={busy || !form.offer || !catalog ||
                  references.some(({ key, options }) => !options?.some((item) => String(item.id) === form[key])) ||
                  result?.status === 'COMPLETED'}>
                {busy ? 'Проверяем Keitaro…' : result?.status === 'UNCERTAIN' ? 'Сверить результат' : 'Создать кампанию'}
              </button>} />
          </div>
        </form>
        <section className="panel flow-preview" aria-label="Потоки новой кампании">
          <h2>Маршрутизация</h2>
          <ol>
            <li><strong>{form.geo || 'Указанная страна'}</strong><span>Редирект на Google</span></li>
            <li><strong>Остальной трафик</strong><span>{form.offer?.name || 'Выбранный оффер'}</span></li>
          </ol>
          {result?.status === 'COMPLETED' && result.campaign_id !== null && (
            <div className="notice notice--success" role="status">
              <strong>Кампания #{result.campaign_id} создана</strong>
              <HelpHint label="Открыть в редакторе" text="Перейдите к этой кампании и загрузите её потоки из Keitaro."
                control={<button className="button button--subtle" onClick={() => openEditor(result.campaign_id!)}>
                  Открыть в редакторе
                </button>} />
            </div>
          )}
          {result?.status === 'UNCERTAIN' && (
            <div className="notice notice--warning" role="status">
              <strong>Результат создания требует сверки</strong>
              <span>Алиас {result.alias}{result.campaign_id && ` · кампания #${result.campaign_id}`}</span>
              <span>Этап операции — {result.stage}. Повторная сверка только читает Keitaro.</span>
            </div>
          )}
        </section>
      </div>
    </>
  )
}
