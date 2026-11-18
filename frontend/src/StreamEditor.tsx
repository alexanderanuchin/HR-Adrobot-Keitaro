import { type FormEvent, useEffect, useId, useRef, useState } from 'react'
import {
  type Campaign, type Draft, errorText, type Item, type Position, type Stream, workspace,
} from './api'
import HelpHint, { FieldHelp } from './HelpHint'
import OfferPicker from './OfferPicker'

const labels: Record<string, string> = {
  SYNCED: 'Синхронизирован',
  DIRTY: 'Есть изменения',
  CONFLICT: 'Конфликт',
  PUBLISHING: 'Публикация',
  VERIFYING: 'Сверка',
  UNCERTAIN: 'Нужна сверка',
  FAILED: 'Ошибка',
  PUBLISHED: 'Опубликован',
}

type CampaignPage = { items: Item[]; next_offset: number | null }
type EditAction = 'remove' | 'bring_back' | 'pin'
type DraftConfirmation = 'publish' | 'cancel'
const fetchLabel = 'Загрузить потоки из Keitaro'

function ConfirmDraftAction({ action, uncertain, onClose, onConfirm }: {
  action: DraftConfirmation
  uncertain: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const descriptionId = useId()
  const publishing = action === 'publish'

  useEffect(() => {
    const element = dialog.current
    element?.showModal()
    return () => element?.close()
  }, [])

  return (
    <dialog ref={dialog} className="confirm-dialog" aria-labelledby={titleId} aria-describedby={descriptionId}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}>
      <div className="form-stack">
        <h2 id={titleId}>{publishing ? 'Опубликовать офферы?' : 'Отменить правки?'}</h2>
        <p id={descriptionId}>
          {publishing ? 'Состав офферов и их доли обновятся в этом потоке Keitaro.'
            : uncertain ? 'Результат публикации ещё не подтверждён. Перед отменой проверьте список офферов в Keitaro.'
              : 'Текущий черновик будет сброшен до данных последней сверки с Keitaro.'}
        </p>
        <div className="panel-actions">
          <button type="button" className="button button--subtle" onClick={onClose} autoFocus>Назад</button>
          <button type="button" className={`button ${publishing ? 'button--primary' : 'button--danger'}`}
            onClick={onConfirm}>{publishing ? 'Опубликовать' : 'Сбросить черновик'}</button>
        </div>
      </div>
    </dialog>
  )
}

function ShareControl({ position, disabled, save }: {
  position: Position
  disabled: boolean
  save: (percent: number | null) => void
}) {
  const id = useId()
  const [percent, setPercent] = useState(String(position.pinned_percent ?? position.calculated_percent))
  return (
    <form className="pin-form" onSubmit={(event) => {
      event.preventDefault()
      save(Number(percent))
    }}>
      <div className="field pin-value">
        <FieldHelp id={id} label="Доля, %" text="Введите целое число от 0 до 100 и нажмите «Закрепить долю»." />
        <input id={id} type="number" min={0} max={100} step={1} required value={percent} disabled={disabled}
          onChange={(event) => setPercent(event.target.value)} />
      </div>
      <HelpHint label="Закрепить долю" text="Оффер сохранит этот процент. Остальные офферы без фиксации поделят остаток."
        control={<button className="button button--subtle" disabled={disabled || Number(percent) === position.pinned_percent}>
          Закрепить долю
        </button>} />
      {position.pinned_percent !== null && (
        <HelpHint label="Снять фиксацию" text="Доля оффера снова будет рассчитываться вместе с остальными."
          control={<button type="button" className="button button--subtle" disabled={disabled} onClick={() => save(null)}>
            Снять фиксацию
          </button>} />
      )}
    </form>
  )
}

export default function StreamEditor({ campaignId, onCampaign }: {
  campaignId: number | null
  onCampaign: (id: number | null) => void
}) {
  const [campaigns, setCampaigns] = useState<Item[]>([])
  const [nextOffset, setNextOffset] = useState<number | null>(0)
  const [catalogError, setCatalogError] = useState('')
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [manualId, setManualId] = useState('')
  const [selected, setSelected] = useState<Stream | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [offer, setOffer] = useState<Item | null>(null)
  const [names, setNames] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmation, setConfirmation] = useState<DraftConfirmation | null>(null)

  function addCampaigns(items: Item[]) {
    setCampaigns((previous) => Array.from(new Map([...previous, ...items].map((item) => [item.id, item])).values()))
  }

  useEffect(() => {
    const controller = new AbortController()
    workspace<Item[]>('campaigns', undefined, controller.signal)
      .then(addCampaigns)
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setCatalogError(errorText(requestError))
      })
    workspace<CampaignPage>('campaigns/remote', undefined, controller.signal)
      .then((page) => {
        addCampaigns(page.items)
        setNextOffset(page.next_offset)
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setCatalogError(errorText(requestError))
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    setCampaign(null)
    setSelected(null)
    setDraft(null)
    setOffer(null)
    if (campaignId === null) {
      setBusy('')
      return
    }
    const controller = new AbortController()
    setBusy('Читаем сохранённые потоки…')
    workspace<Campaign>(`campaigns/${campaignId}`, undefined, controller.signal)
      .then(setCampaign)
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setError(errorText(requestError))
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy('')
      })
    return () => controller.abort()
  }, [campaignId])

  const offerIds = [...new Set(draft?.positions.map((position) => position.keitaro_offer_id))].sort((a, b) => a - b)
  const lookup = offerIds.slice(0, 90).join(',')
  useEffect(() => {
    if (!lookup) return
    const controller = new AbortController()
    workspace<Item[]>('offers', undefined, controller.signal, new URLSearchParams({ ids: lookup }))
      .then((items) => {
        if (!controller.signal.aborted) {
          setNames((previous) => ({ ...previous, ...Object.fromEntries(items.map((item) => [item.id, item.name])) }))
        }
      })
      .catch(() => {})
    return () => controller.abort()
  }, [lookup])

  async function run(label: string, operation: () => Promise<void>) {
    if (busy) return
    setBusy(label)
    setError('')
    setNotice('')
    try {
      await operation()
    } catch (requestError) {
      setError(errorText(requestError))
    } finally {
      setBusy('')
    }
  }

  function selectCampaign(id: number | null) {
    setError('')
    setNotice('')
    onCampaign(id)
  }

  function enterId(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const id = Number(manualId)
    if (Number.isSafeInteger(id) && id > 0) selectCampaign(id)
    else setError('Введите положительный целочисленный ID кампании.')
  }

  async function moreCampaigns() {
    if (nextOffset === null) return
    await run('Загружаем кампании…', async () => {
      const page = await workspace<CampaignPage>('campaigns/remote', undefined, undefined,
        new URLSearchParams({ offset: String(nextOffset) }))
      addCampaigns(page.items)
      setNextOffset(page.next_offset)
      setCatalogError('')
    })
  }

  async function openStream(stream: Stream) {
    await run('Открываем черновик…', async () => {
      const view = await workspace<Draft>(`streams/${stream.id}/workspace`, {})
      setSelected(stream)
      setDraft(view)
      setOffer(null)
    })
  }

  async function fetchStreams() {
    if (campaignId === null) return
    await run('Синхронизируем потоки…', async () => {
      const result = await workspace<Campaign>(`campaigns/${campaignId}/fetch`, {})
      setCampaign(result)
      if (result.name) addCampaigns([{ id: result.id, name: result.name }])
      const current = result.streams.find((stream) => stream.id === selected?.id)
      if (current && (current.editable || current.draft_status)) {
        const view = await workspace<Draft>(`streams/${current.id}/workspace`, {})
        setSelected(current)
        setDraft(view)
      } else {
        setSelected(null)
        setDraft(null)
      }
      setNotice('Потоки сверены с Keitaro.')
    })
  }

  async function addOffer() {
    if (!draft || !offer) return
    await run('Сохраняем оффер в черновике…', async () => {
      const view = await workspace<Draft>(`drafts/${draft.draft_id}/offers`, {
        revision: draft.revision, offer_id: offer.id,
      })
      setNames((previous) => ({ ...previous, [offer.id]: offer.name }))
      setDraft(view)
      setOffer(null)
    })
  }

  async function edit(action: EditAction, positionId: string, percent: number | null = null) {
    if (!draft) return
    await run('Сохраняем черновик…', async () => {
      setDraft(await workspace<Draft>(`drafts/${draft.draft_id}/edit`, {
        revision: draft.revision, action, position_id: positionId, pinned_percent: percent,
      }))
    })
  }

  async function publish() {
    if (!draft) return
    await run('Записываем и проверяем публикацию…', async () => {
      const view = await workspace<Draft>(`drafts/${draft.draft_id}/publish`, { revision: draft.revision })
      setDraft(view)
      if (view.status === 'PUBLISHED') setNotice('Публикация подтверждена чтением потока из Keitaro.')
    })
  }

  async function cancel() {
    if (!draft) return
    const uncertain = draft.status === 'UNCERTAIN'
    await run('Отменяем локальные изменения…', async () => {
      const result = await workspace<{ draft: Draft | null }>(`drafts/${draft.draft_id}/cancel`, {
        revision: draft.revision, confirm_uncertain: uncertain,
      })
      setDraft(result.draft)
      setOffer(null)
      setNotice('Локальные изменения отменены.')
    })
  }

  const editable = Boolean(draft && ['SYNCED', 'DIRTY'].includes(draft.status))
  const activeTotal = draft?.positions.reduce((sum, position) => sum + position.calculated_percent, 0)
  const draftNotice = draft?.reason === 'publication_rejected'
    ? `Keitaro отклонил публикацию (HTTP ${draft.upstream_status}). Черновик сохранён.`
    : draft?.status === 'CONFLICT'
      ? 'Состав офферов изменился в Keitaro. Черновик сохранён. Сверьте поток или отмените локальные изменения, чтобы открыть актуальный список.'
      : draft && ['UNCERTAIN', 'PUBLISHING', 'VERIFYING'].includes(draft.status)
        ? `Результат публикации требует сверки. Нажмите «${fetchLabel}».`
        : ''

  return (
    <>
      <header className="page-heading">
        <h1>Редактор потоков</h1>
        <p>Загрузите потоки, измените офферы в черновике и опубликуйте их в Keitaro.</p>
      </header>
      <section className="panel form-stack">
        <div className="editor-controls">
          <div className="field">
            <FieldHelp id="editor-campaign" label="Кампания"
              text="В списке кампании из Keitaro и те, что уже сохранены локально. Выберите нужную и загрузите её потоки." />
            <select id="editor-campaign" value={campaignId ?? ''} disabled={Boolean(busy)}
              onChange={(event) => selectCampaign(event.target.value ? Number(event.target.value) : null)}>
              <option value="">Выберите кампанию</option>
              {campaignId !== null && !campaigns.some((item) => item.id === campaignId) && (
                <option value={campaignId}>Кампания #{campaignId}</option>
              )}
              {campaigns.map((item) => <option key={item.id} value={item.id}>{item.name} · #{item.id}</option>)}
            </select>
          </div>
          <form className="inline-form" onSubmit={enterId}>
            <div className="field">
              <FieldHelp id="editor-campaign-id" label="Или откройте по ID"
                text="Если кампании пока нет в списке, введите её номер из Keitaro и нажмите «Открыть»." />
              <input id="editor-campaign-id" value={manualId} onChange={(event) => setManualId(event.target.value)} inputMode="numeric"
                pattern="[0-9]+" placeholder="ID кампании" required disabled={Boolean(busy)} />
            </div>
            <button className="button button--subtle" disabled={Boolean(busy)}>Открыть</button>
          </form>
        </div>
        <div className="panel-actions">
          <HelpHint label={fetchLabel} text="Прочитает потоки и офферы в Keitaro. Если черновик уже есть, он сохранится."
            control={<button className="button button--primary" onClick={fetchStreams} disabled={Boolean(busy) || campaignId === null}>
              {fetchLabel}
            </button>} />
          {nextOffset !== null && (
            <HelpHint label="Загрузить список кампаний" text="Добавит в список следующую страницу кампаний из Keitaro."
              control={<button className="button button--subtle" onClick={moreCampaigns} disabled={Boolean(busy)}>
                {nextOffset === 0 ? 'Загрузить кампании Keitaro' : 'Ещё кампании'}
              </button>} />
          )}
        </div>
        {catalogError && <p className="muted">{catalogError}</p>}
      </section>
      <div className="feedback" aria-live="polite">
        {busy && <p className="muted">{busy}</p>}
        {error && <p className="notice notice--error" role="alert">{error}</p>}
        {notice && <p className="notice notice--success">{notice}</p>}
      </div>
      {campaignId !== null && campaign && (
        <section className="panel">
          <h2>{campaign.name || `Кампания #${campaign.id}`}</h2>
          {campaign.streams.length ? (
            <ul className="stream-list">
              {campaign.streams.map((stream) => (
                <li key={stream.id} className={selected?.id === stream.id ? 'stream--selected' : ''}>
                  <div><strong>{stream.name || `Поток #${stream.keitaro_id}`}</strong>
                    <span>#{stream.keitaro_id} · {stream.schema}{!stream.present && ' · отсутствует в Keitaro'}</span></div>
                  {stream.editable || stream.draft_status ? (
                    <HelpHint label="Черновик потока" text="Откроет сохранённый черновик выбранного потока или создаст новый из загруженных офферов."
                      control={<button className="button button--subtle" disabled={Boolean(busy)} onClick={() => openStream(stream)}>
                        {selected?.id === stream.id ? 'Обновить черновик' : 'Открыть офферы'}
                      </button>} />
                  ) : <span className="muted">Только просмотр</span>}
                </li>
              ))}
            </ul>
          ) : <p className="muted">Потоки ещё не загружены. Нажмите «{fetchLabel}».</p>}
        </section>
      )}
      {draft && (
        <section className="panel form-stack draft" aria-label="Черновик офферов">
          <div className="panel-heading">
            <h2>{selected?.name || 'Черновик офферов'}</h2>
            <HelpHint label="Состояние черновика"
              text="«Есть изменения» означает, что правки сохранены в черновике. Чтобы применить их, нажмите «Опубликовать в Keitaro»."
              control={<span className={`status status--${draft.status.toLowerCase()}`}>{labels[draft.status] || draft.status}</span>} />
          </div>
          {draftNotice && <p className="notice notice--warning" role="status">{draftNotice}</p>}
          {draft.positions.length ? (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Оффер</th><th>Доля</th><th>Действия</th></tr></thead>
                <tbody>
                  {draft.positions.map((position) => (
                    <tr key={position.position_id} className={position.status === 'REMOVED' ? 'row--archived' : ''}>
                      <td>
                        <strong>{names[position.keitaro_offer_id] || 'Оффер'}</strong>
                        <small>#{position.keitaro_offer_id}{position.status === 'REMOVED' && ' · архив'}</small>
                      </td>
                      <td className="share">
                        {position.calculated_percent}%{position.pinned_percent !== null && <small>Закреплено {position.pinned_percent}%</small>}
                      </td>
                      <td>
                        <div className="row-actions">
                          {position.status === 'REMOVED' ? (
                            <HelpHint label="Вернуть из архива"
                              text="Вернёт оффер в черновик потока и пересчитает доли. Если доля была закреплена, фиксация восстановится."
                              control={<button className="button button--subtle" disabled={!editable || Boolean(busy)}
                                onClick={() => edit('bring_back', position.position_id)}>Вернуть из архива</button>} />
                          ) : (
                            <>
                              <ShareControl key={`${position.position_id}:${draft.revision}`} position={position} disabled={!editable || Boolean(busy)}
                                save={(percent) => edit('pin', position.position_id, percent)} />
                              <HelpHint label="Убрать из потока"
                                text="Перенесёт позицию в архив с долей 0%. Вернуть её можно кнопкой «Вернуть из архива»."
                                control={<button className="button button--danger" disabled={!editable || Boolean(busy)}
                                  onClick={() => edit('remove', position.position_id)}>Убрать из потока</button>} />
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot><tr><th>Всего</th><td>{activeTotal}%</td><td /></tr></tfoot>
              </table>
            </div>
          ) : <p className="muted">Офферов в потоке пока нет.</p>}
          {editable && (
            <div className="add-offer">
              <OfferPicker value={offer} onPick={setOffer} disabled={Boolean(busy)}
                help="Найдите активный оффер Keitaro по названию или ID. Затем выберите его и нажмите «Добавить оффер»." />
              <HelpHint label="Добавить оффер" text="Добавит выбранный оффер в черновик и пересчитает доли."
                control={<button className="button button--subtle" disabled={Boolean(busy) || !offer}
                  onClick={addOffer}>Добавить оффер</button>} />
            </div>
          )}
          <div className="panel-actions">
            {draft.status === 'PUBLISHED' ? (
              <HelpHint label="Открыть текущие офферы" text="Откроет новый черновик на основе только что опубликованных офферов."
                control={<button className="button button--subtle" disabled={Boolean(busy) || !selected}
                  onClick={() => selected && openStream(selected)}>Открыть текущие офферы</button>} />
            ) : (
              <>
                <HelpHint label="Опубликовать в Keitaro"
                  text="Проверит актуальный поток и после подтверждения запишет состав офферов и их доли в Keitaro."
                  control={<button className="button button--primary" disabled={Boolean(busy) || draft.status !== 'DIRTY'}
                    onClick={() => setConfirmation('publish')}>Опубликовать в Keitaro</button>} />
                <HelpHint label="Отменить правки" text="Сбросит неопубликованные правки и откроет данные последней сверки с Keitaro."
                  control={<button className="button button--subtle"
                    disabled={Boolean(busy) || ['SYNCED', 'PUBLISHING', 'VERIFYING'].includes(draft.status) ||
                      (draft.status === 'UNCERTAIN' && !draft.reconciliation_checked)}
                    onClick={() => setConfirmation('cancel')}>Отменить правки</button>} />
              </>
            )}
          </div>
        </section>
      )}
      {draft && confirmation && (
        <ConfirmDraftAction action={confirmation} uncertain={draft.status === 'UNCERTAIN'}
          onClose={() => setConfirmation(null)} onConfirm={() => {
            setConfirmation(null)
            void (confirmation === 'publish' ? publish() : cancel())
          }} />
      )}
    </>
  )
}
