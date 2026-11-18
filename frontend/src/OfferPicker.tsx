import { useEffect, useId, useState } from 'react'
import { errorText, type Item, workspace } from './api'
import HelpHint, { FieldHelp } from './HelpHint'

const pageSize = 50

export default function OfferPicker({
  value, onPick, disabled, help, allowBrowsing,
}: {
  value: Item | null
  onPick: (offer: Item | null) => void
  disabled?: boolean
  help: string
  allowBrowsing?: boolean
}) {
  const id = useId()
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<Item[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  const [browse, setBrowse] = useState(false)
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const normalized = query.trim()
  const showResults = Boolean(normalized) || browse

  function resetResults(loading = false) {
    setMatches([])
    setError('')
    setOffset(0)
    setHasMore(false)
    setSearching(loading)
  }

  useEffect(() => {
    if (!showResults || value) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      workspace<Item[]>('offers', undefined, controller.signal,
        new URLSearchParams({ q: normalized, offset: String(offset) }))
        .then((page) => {
          if (controller.signal.aborted) return
          setMatches((previous) => offset === 0 ? page : [...previous, ...page])
          setHasMore(page.length === pageSize)
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) setError(errorText(error))
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false)
        })
    }, normalized && offset === 0 ? 250 : 0)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [normalized, offset, showResults, value])

  return (
    <div className="offer-picker">
      {value ? (
        <div className="selected-offer">
          <span><strong>{value.name}</strong> <small>#{value.id}</small></span>
          <HelpHint label="Заменить оффер" text="Откроет поиск, чтобы вы могли выбрать другой оффер."
            control={<button type="button" className="button button--subtle" disabled={disabled} onClick={() => {
              setQuery('')
              resetResults()
              onPick(null)
            }}>Заменить</button>} />
        </div>
      ) : (
        <>
          <div className="field">
            <FieldHelp id={id} label="Поиск оффера" text={help} />
            <input id={id} type="search" value={query} maxLength={255} placeholder="Название или ID Keitaro" disabled={disabled}
              onChange={(event) => {
                const next = event.target.value
                const nextSearch = next.trim()
                if (nextSearch !== normalized || browse) resetResults(Boolean(nextSearch))
                setBrowse(false)
                setQuery(next)
              }} />
          </div>
          {allowBrowsing && !query && (
            <HelpHint label="Список офферов" text="Показывает активные офферы, доступные текущему ключу Keitaro."
              control={<button type="button" className="button button--subtle" disabled={disabled} onClick={() => {
                setBrowse(!browse)
                resetResults(!browse)
              }}>{browse ? 'Скрыть список офферов' : 'Показать все офферы'}</button>} />
          )}
          {showResults && (
            <div className="offer-results" aria-live="polite">
              {matches.map((offer) => (
                <button key={offer.id} type="button" disabled={disabled} onClick={() => {
                  setQuery('')
                  setBrowse(false)
                  resetResults()
                  onPick(offer)
                }}>
                  <span>{offer.name}</span><small>#{offer.id}</small>
                </button>
              ))}
              {error && <p className="muted">{error}</p>}
              {searching && <p className="muted">Загрузка офферов из Keitaro…</p>}
              {!error && !searching && !matches.length && <p className="muted">Офферы не найдены</p>}
              {hasMore && !error && !searching && (
                <button type="button" disabled={disabled} onClick={() => {
                  setSearching(true)
                  setOffset(offset + pageSize)
                }}>Ещё офферы</button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
