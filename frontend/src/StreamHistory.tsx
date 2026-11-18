import { useEffect, useState } from 'react'
import { errorText, type StreamHistoryPage, workspace } from './api'
import HelpHint from './HelpHint'
import { streamStatusLabels } from './streamStatus'

const editDate = new Intl.DateTimeFormat('ru', { dateStyle: 'short', timeStyle: 'short' })

export default function StreamHistory({ onOpen }: { onOpen: (campaignId: number, streamId: number) => void }) {
  const [items, setItems] = useState<StreamHistoryPage['items']>([])
  const [offset, setOffset] = useState(0)
  const [nextOffset, setNextOffset] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [attempt, setAttempt] = useState(0)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    workspace<StreamHistoryPage>('streams/history', undefined, controller.signal,
      new URLSearchParams({ offset: String(offset) }))
      .then((page) => {
        if (controller.signal.aborted) return
        setItems((previous) => offset === 0 ? page.items : Array.from(
          new Map([...previous, ...page.items].map((item) => [item.stream_id, item])).values(),
        ))
        setNextOffset(page.next_offset)
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setError(errorText(requestError))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [offset, attempt])

  return (
    <>
      <header className="page-heading">
        <h1>История потоков</h1>
        <p>Откройте поток, чтобы продолжить работу с ним. Последние правки показаны первыми.</p>
      </header>
      <section className="panel form-stack stream-history" aria-label="Редактировавшиеся потоки">
        {items.length > 0 && (
          <ul className="stream-list">
            {items.map((item) => (
              <li key={item.stream_id}>
                <div>
                  <strong>{item.stream_name || `Поток #${item.stream_id}`}</strong>
                  <span>#{item.stream_id} · {item.campaign_name || `Кампания #${item.campaign_id}`}</span>
                  <span>Последняя правка <time dateTime={item.edited_at}>{editDate.format(new Date(item.edited_at))}</time></span>
                </div>
                <div className="history-actions">
                  <span className={`status status--${item.status?.toLowerCase() || 'synced'}`}>
                    {item.can_open ? (streamStatusLabels[item.status ?? 'SYNCED'] ?? 'Можно открыть') : 'Недоступен'}
                  </span>
                  {item.can_open && <HelpHint label="Открыть поток из истории"
                    text="Откроет этот поток с сохранённым черновиком."
                    control={<button type="button" className="button button--subtle"
                      aria-label={`Открыть поток #${item.stream_id}`}
                      onClick={() => onOpen(item.campaign_id, item.stream_id)}>Открыть поток</button>} />}
                </div>
              </li>
            ))}
          </ul>
        )}
        {loading && <p className="muted" role="status">Загружаем историю…</p>}
        {!items.length && !loading && !error && <p className="muted">Здесь появятся потоки после первой правки в редакторе.</p>}
        {error && <p className="notice notice--error" role="alert">{error}</p>}
        {error && <HelpHint label="Повторить" text="Заново загрузит список потоков из истории."
          control={<button type="button" className="button button--subtle" onClick={() => setAttempt(attempt + 1)}>Повторить</button>} />}
        {nextOffset !== null && !error && <button type="button" className="button button--subtle" disabled={loading}
          onClick={() => setOffset(nextOffset)}>Ещё потоки</button>}
      </section>
    </>
  )
}
