import { useEffect, useState } from 'react'
import { errorText, type Item, workspace } from './api'

export default function OfferPicker({
  value, onPick, disabled, required,
}: {
  value: Item | null
  onPick: (offer: Item | null) => void
  disabled?: boolean
  required?: boolean
}) {
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<Item[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const normalized = query.trim()
    if (!normalized || value) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      setSearching(true)
      workspace<Item[]>('offers', undefined, controller.signal, new URLSearchParams({ q: normalized }))
        .then(setMatches)
        .catch((error: unknown) => {
          if (!controller.signal.aborted) setError(errorText(error))
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false)
        })
    }, 250)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [query, value])

  return (
    <div className="offer-picker">
      {value ? (
        <div className="selected-offer">
          <span><strong>{value.name}</strong> <small>#{value.id}</small></span>
          <button type="button" className="button button--subtle" disabled={disabled} onClick={() => {
            setQuery('')
            setMatches([])
            onPick(null)
          }}>Заменить</button>
        </div>
      ) : (
        <>
          <label className="field">
            <span>Поиск оффера {required && <span aria-hidden="true">*</span>}</span>
            <input type="search" value={query} maxLength={255} placeholder="Название или ID Keitaro" disabled={disabled}
              onChange={(event) => {
                setMatches([])
                setError('')
                setSearching(Boolean(event.target.value.trim()))
                setQuery(event.target.value)
              }} />
          </label>
          {query.trim() && (
            <div className="offer-results" aria-live="polite">
              {error ? <p className="muted">{error}</p> : searching ? (
                <p className="muted">Поиск в Keitaro…</p>
              ) : matches.length ? (
                matches.map((offer) => (
                  <button key={offer.id} type="button" disabled={disabled} onClick={() => onPick(offer)}>
                    <span>{offer.name}</span><small>#{offer.id}</small>
                  </button>
                ))
              ) : <p className="muted">Офферы не найдены</p>}
            </div>
          )}
        </>
      )}
    </div>
  )
}
