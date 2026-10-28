import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { fmt } from '@/lib/format'
import { llm, type PromptInfo } from '@/screens/ai/api'
import { ErrorNotice, KV, Panel } from '@/ui/kit'
import { Sheet } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'

const PRE =
  'num overflow-auto rounded-md border border-hairline bg-canvas p-3 text-body-compact whitespace-pre-wrap text-ink-muted'

const COLUMNS: Column<PromptInfo>[] = [
  {
    key: 'label',
    header: 'Prompt',
    width: 'minmax(160px,1fr)',
    cell: (p) => <span className="text-ink">{p.label}</span>,
  },
  {
    key: 'purpose',
    header: 'What it does',
    width: 'minmax(240px,3fr)',
    cell: (p) => <span className="truncate text-ink-subtle">{p.purpose}</span>,
  },
  {
    key: 'characters',
    header: 'Characters',
    width: '100px',
    align: 'right',
    cell: (p) => fmt.int(p.characters),
  },
  {
    key: 'tokens',
    header: '~Tokens',
    width: '90px',
    align: 'right',
    cell: (p) => fmt.int(p.estimatedTokens),
  },
]

export function Prompts() {
  const prompts = useQuery({
    queryKey: ['ai', 'prompts'],
    queryFn: llm.prompts,
  })
  const [open, setOpen] = useState<PromptInfo | null>(null)

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Prompts"
        description="Every instruction the app sends a model, word for word. Click one to read it."
        bodyClassName="p-0"
      >
        {prompts.isError ? (
          <ErrorNotice className="m-4" title="Could not load the prompts" error={prompts.error} />
        ) : (
          <DataTable
            label="Prompts"
            rows={prompts.data?.prompts ?? []}
            columns={COLUMNS}
            rowKey={(p) => p.slug}
            loading={prompts.isLoading}
            onRowClick={setOpen}
            empty="No prompts."
          />
        )}
      </Panel>

      <Sheet
        open={open !== null}
        onOpenChange={(o) => !o && setOpen(null)}
        title={open?.label}
        description={open?.purpose}
      >
        {open && (
          <div className="flex flex-col gap-3">
            <KV
              items={[
                ['Slug', open.slug],
                [
                  'Size',
                  `${fmt.int(open.characters)} chars · ~${fmt.int(open.estimatedTokens)} tokens`,
                ],
              ]}
            />
            <section aria-label="Prompt text">
              <pre className={PRE}>{open.body}</pre>
            </section>
          </div>
        )}
      </Sheet>
    </div>
  )
}
