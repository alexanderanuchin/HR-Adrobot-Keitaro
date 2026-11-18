import { type ReactNode, useId, useState } from 'react'

export default function HelpHint({ label, control, text }: {
  label: string
  control?: ReactNode
  text: string
}) {
  const id = useId()
  const [open, setOpen] = useState(false)

  return (
    <div className="hint" onKeyDown={(event) => {
      if (event.key === 'Escape' && open) {
        event.stopPropagation()
        setOpen(false)
      }
    }}>
      <div className="hint__line">
        {control}
        <button className="hint__button" type="button" aria-label={`Подсказка: ${label}`}
          aria-expanded={open} aria-controls={id} onClick={() => setOpen((value) => !value)}>?</button>
      </div>
      <p className="hint__body" id={id} hidden={!open}>{text}</p>
    </div>
  )
}

export function FieldHelp({ id, label, text }: { id: string; label: string; text: string }) {
  return <HelpHint label={label} control={<label htmlFor={id}>{label}</label>} text={text} />
}
