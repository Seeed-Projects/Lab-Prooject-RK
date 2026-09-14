export const defaultVoicePresentation = Object.freeze({
  summary: 'Waiting for conversation',
  intent: 'Available after transcript',
  recommendation: 'Available after transcript',
})

const emptyValues = new Set(['', 'null', 'none', 'undefined', '[object object]'])

function cleanText(value) {
  if (value == null || typeof value === 'object') return ''
  const text = String(value).trim()
  return emptyValues.has(text.toLowerCase()) ? '' : text.replace(/^`+|`+$/g, '').trim()
}

function parseJsonText(text) {
  const unfenced = text
    .trim()
    .replace(/^```(?:json)?\s*/i, '')
    .replace(/\s*```$/, '')
    .trim()
  const start = unfenced.indexOf('{')
  const end = unfenced.lastIndexOf('}')
  const candidates = [unfenced, start >= 0 && end > start ? unfenced.slice(start, end + 1) : '']
  for (const candidate of candidates) {
    if (!candidate) continue
    try {
      return JSON.parse(candidate)
    } catch {
      // Try the next representation.
    }
  }
  return null
}

function parseLabeledSections(text) {
  const labelPattern = /(?:^|\n)\s*(?:#+\s*)?(conversation\s+summary|summary|customer\s+intent|intent|recommendation|recommended\s+action|next\s+step)\s*[:\-]\s*/gi
  const matches = [...text.matchAll(labelPattern)]
  const result = {}
  matches.forEach((match, index) => {
    const label = match[1].toLowerCase()
    const field = label.includes('intent')
      ? 'intent'
      : label.includes('recommend') || label.includes('next')
        ? 'recommendation'
        : 'summary'
    const start = match.index + match[0].length
    const end = index + 1 < matches.length ? matches[index + 1].index : text.length
    const value = text.slice(start, end).replace(/^[\s*#-]+|[\s*#-]+$/g, '')
    if (value) result[field] = value
  })
  return result
}

function fieldText(value, field, fallback, depth) {
  const direct = cleanText(value)
  if (direct) {
    const parsed = parseJsonText(direct)
    if (parsed != null && parsed !== value) return normalizeVoiceAnalysis(parsed, fallback, depth + 1)[field]
    return direct
  }
  if (Array.isArray(value)) return value.map(cleanText).filter(Boolean).join(' ')
  if (value && typeof value === 'object') return normalizeVoiceAnalysis(value, fallback, depth + 1)[field]
  return ''
}

export function normalizeVoiceAnalysis(input, fallback = defaultVoicePresentation, depth = 0) {
  const defaults = { ...fallback }
  if (input == null || depth > 6) return defaults

  let value = input
  if (typeof value === 'string') {
    const parsed = parseJsonText(value)
    if (parsed != null && parsed !== value) return normalizeVoiceAnalysis(parsed, defaults, depth + 1)
    const sections = parseLabeledSections(value)
    if (Object.keys(sections).length) value = sections
    else {
      const summary = cleanText(value)
      return summary ? { ...defaults, summary } : defaults
    }
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const nested = normalizeVoiceAnalysis(item, defaults, depth + 1)
      if (JSON.stringify(nested) !== JSON.stringify(defaults)) return nested
    }
    return defaults
  }
  if (!value || typeof value !== 'object') return defaults

  const aliases = {
    summary: ['summary', 'conversation_summary', 'overview'],
    intent: ['intent', 'customer_intent', 'customerIntent'],
    recommendation: ['recommendation', 'recommendations', 'recommended_action', 'next_action', 'next_step'],
  }
  const result = {}
  for (const [field, keys] of Object.entries(aliases)) {
    for (const key of keys) {
      if (!(key in value)) continue
      const text = fieldText(value[key], field, defaults, depth)
      if (text) result[field] = text
      break
    }
  }
  if (Object.keys(result).length) return { ...defaults, ...result }

  for (const key of ['analysis', 'result', 'data', 'output', 'response', 'choices', 'message', 'content', 'text']) {
    if (!(key in value)) continue
    const nested = normalizeVoiceAnalysis(value[key], defaults, depth + 1)
    if (JSON.stringify(nested) !== JSON.stringify(defaults)) return nested
  }
  return defaults
}
