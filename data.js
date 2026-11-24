/*
 * CASE-OPS demo dataset (State vs. Alex Miller, NY-2026-CR). Replace with API responses.
 *
 * Integrity rule: contradiction quotes, timeline sources and refs are DERIVED from `proofs`
 * (proofId + line index), so every insight stays traceable to its raw source text.
 * Legal rules below are PLACEHOLDERS for the prototype. Have counsel verify before real use.
 */
window.CASE_DATA = {
  title: 'State vs. Alex Miller',
  docket: 'NY-2026-CR',
  tz: 'America/New_York',

  files: [
    { id: 'f1', name: 'ezpass_ledger_0314.pdf', type: 'PDF', status: 'OCR Parsed', tone: 'info' },
    { id: 'f2', name: 'detective_miller_report.pdf', type: 'PDF', status: 'OCR Parsed', tone: 'info' },
    { id: 'f3', name: 'voicemail_0314_2150.wav', type: 'WAV', status: 'Transcribed', tone: 'ok' },
    { id: 'f4', name: 'witness_vance_deposition.pdf', type: 'PDF', status: 'OCR Parsed', tone: 'info' },
    { id: 'f5', name: 'whatsapp_miller_vance.txt', type: 'TXT', status: 'OCR Parsed', tone: 'info' },
    { id: 'f6', name: 'arraignment_order.pdf', type: 'PDF', status: 'OCR Parsed', tone: 'info' },
  ],

  /* kind: 'doc' | 'audio'. hl = index of the default highlighted line (-1 = none). bbox = [x1,y1,x2,y2]. */
  proofs: {
    'p-ezpass': {
      kind: 'doc', file: 'ezpass_ledger_0314.pdf', loc: 'p. 2', where: 'Page 2',
      insight: 'Yonkers Toll Plaza E-ZPass Scan', conf: 99.2, confKind: 'OCR',
      exhibit: 'EXHIBIT D-14 • BATES NY26-00412', pageLabel: 'PAGE 2 OF 4', hl: 2,
      lines: [
        { t: 'NEW YORK STATE THRUWAY AUTHORITY — ELECTRONIC TOLL LOG' },
        { t: 'TAG ID: 004-8829104 • ACCOUNT: MILLER, ALEX (SILVER SEDAN)' },
        { t: '2026-03-14 21:42:18 EDT | LANE 04-N | YONKERS PLAZA | $2.75', bbox: [112, 340, 520, 384] },
        { t: 'IMAGE CAPTURE CONFIRMED — REAR PLATE NY-KLM4821' },
      ],
    },
    'p-detective': {
      kind: 'doc', file: 'detective_miller_report.pdf', loc: 'p. 4', where: 'Page 4',
      insight: 'Detective Brooks Arrest Narrative', conf: 96.8, confKind: 'OCR',
      exhibit: 'EXHIBIT P-02 • BATES NY26-00108', pageLabel: 'PAGE 4 OF 9', hl: 2,
      lines: [
        { t: 'NYPD SUPPLEMENTAL COMPLAINT REPORT — PRECINCT 14' },
        { t: 'OBSERVATION: Suspect vehicle observed parked outside 44th St.' },
        { t: 'At 21:53 EDT, defendant Alex Miller exited a dark blue SUV at Midtown.', bbox: [88, 210, 540, 264] },
        { t: 'Det. R. Brooks #4419 — Signed and notarized 2026-03-15.' },
      ],
    },
    'p-voicemail': {
      kind: 'audio', file: 'voicemail_0314_2150.wav', channel: 'Mono 16 kHz',
      insight: 'Voicemail from Alex Miller to Elena Vance', conf: 98.1, confKind: 'ASR', dur: 78, hl: 2,
      lines: [
        { sec: 12, t: 'Hey Elena, it is Alex. Just got off the Thruway exit.' },
        { sec: 28, t: 'Traffic near Yonkers was backed up twenty minutes.' },
        { sec: 42, t: 'I am still north of the bridge at 21:50, I will not make Midtown before 22:20.' },
        { sec: 64, t: 'Call me back when you get inside.' },
      ],
    },
    'p-vance': {
      kind: 'doc', file: 'witness_vance_deposition.pdf', loc: 'p. 11', where: 'Page 11',
      insight: 'Elena Vance Sworn Deposition', conf: 97.9, confKind: 'OCR',
      exhibit: 'EXHIBIT D-08 • BATES NY26-00891', pageLabel: 'PAGE 11 OF 28', hl: 1,
      lines: [
        { t: 'Q: Ms. Vance, what vehicle did you see Mr. Miller driving that evening?' },
        { t: 'A: He always drives the silver four-door sedan, plate KLM4821.', bbox: [96, 480, 535, 530] },
        { t: 'Q: Did he arrive at 44th Street before 10:00 PM?' },
        { t: 'A: No, he called from Yonkers and arrived around 10:25 PM.' },
      ],
    },
    'p-whatsapp': {
      kind: 'doc', file: 'whatsapp_miller_vance.txt', loc: 'line 142', where: 'Line 142',
      insight: 'WhatsApp Departure Message', conf: null, confKind: 'Native text • no OCR',
      exhibit: 'EXHIBIT D-19 • CHAT EXPORT', pageLabel: 'LINE 142 OF 310', hl: 1,
      lines: [
        { t: '[2026-03-14, 21:15:02] Elena Vance: Did you leave Yonkers yet?' },
        { t: '[2026-03-14, 21:18:04] Alex Miller: Leaving Yonkers now, silver sedan.', bbox: [64, 190, 510, 230] },
        { t: '[2026-03-14, 21:44:10] Alex Miller: Just cleared the toll plaza.' },
      ],
    },
    'p-arraignment': {
      kind: 'doc', file: 'arraignment_order.pdf', loc: 'p. 1', where: 'Page 1',
      insight: 'Trigger Event: Criminal Court Arraignment', conf: 99.6, confKind: 'OCR',
      exhibit: 'COURT ORDER • NY-2026-CR', pageLabel: 'PAGE 1 OF 2', hl: 2,
      lines: [
        { t: 'CRIMINAL COURT OF THE CITY OF NEW YORK — PART A' },
        { t: 'DOCKET NO: NY-2026-CR • DEFENDANT: MILLER, ALEX' },
        { t: 'ARRAIGNMENT ENTERED: 2026-10-14. Statutory 45-day omnibus motion clock commences.', bbox: [102, 280, 512, 326] },
        { t: 'ORDERED: Discovery compliance per CPL § 245.10.' },
      ],
    },
  },

  /* status: 'verified' | 'conflict' | 'pending'. `at` is an ISO instant with offset (EDT = -04:00 on 2026-03-14). */
  events: [
    { id: 'ev1', title: 'WhatsApp Departure Confirmation', at: '2026-03-14T21:18:04-04:00', actors: 'Alex Miller, Elena Vance', proofId: 'p-whatsapp', status: 'verified' },
    { id: 'ev2', title: 'Yonkers Toll Plaza E-ZPass Scan', at: '2026-03-14T21:42:18-04:00', actors: 'Alex Miller (Plate NY-KLM4821)', proofId: 'p-ezpass', status: 'conflict' },
    { id: 'ev3', title: 'Voicemail Location Timestamp', at: '2026-03-14T21:50:11-04:00', actors: 'Alex Miller → Elena Vance', proofId: 'p-voicemail', status: 'verified' },
    { id: 'ev4', title: 'Midtown Arrival Claim (Detective Report)', at: '2026-03-14T21:53:00-04:00', actors: 'Det. R. Brooks, Alex Miller', proofId: 'p-detective', status: 'conflict' },
    { id: 'ev5', title: 'Sworn Witness Vehicle & Arrival Account', at: '2026-03-14T22:25:00-04:00', actors: 'Elena Vance', proofId: 'p-vance', status: 'verified' },
  ],

  contradictions: [
    {
      id: 'c1', title: 'Impossible Travel Time (11 Minutes / 18.4 Miles)',
      a: { proofId: 'p-ezpass', line: 2 }, b: { proofId: 'p-detective', line: 2 },
      summary: 'E-ZPass toll capture at Yonkers Plaza occurs 11 minutes before Det. Brooks places the defendant in Midtown Manhattan (minimum 34-minute drive).',
    },
    {
      id: 'c2', title: 'Conflicting Vehicle Description (Silver Sedan vs. Dark Blue SUV)',
      a: { proofId: 'p-vance', line: 1 }, b: { proofId: 'p-detective', line: 2 },
      summary: 'Sworn deposition and the E-ZPass account identify a silver sedan, directly contradicting the arrest report observation of a dark blue SUV.',
    },
    {
      id: 'c3', title: 'Audio Alibi vs. Arrest Timeline',
      a: { proofId: 'p-voicemail', line: 2 }, b: { proofId: 'p-detective', line: 2 },
      summary: 'The voicemail places the speaker north of the bridge at 21:50, 3 minutes before the reported Midtown sighting at 21:53.',
    },
  ],

  /* PLACEHOLDER legal rules: verify with counsel. Exactly one of `days` or `months` per rule. */
  deadlines: [
    { id: 'd1', title: 'Omnibus Motion', citation: 'CPL § 255.20(1)', rule: { days: 45 }, trigger: { label: 'Arraignment', date: '2026-10-14', proofId: 'p-arraignment' } },
    { id: 'd2', title: 'Discovery Demand', citation: 'CPL § 245.10(1)(a)', rule: { days: 53 }, trigger: { label: 'Arraignment', date: '2026-10-14', proofId: 'p-arraignment' } },
    { id: 'd3', title: 'Speedy Trial', citation: 'CPL § 30.30(1)(a)', rule: { months: 6 }, trigger: { label: 'Arraignment', date: '2026-10-14', proofId: 'p-arraignment' } },
  ],

  ledger: [
    { ts: '2026-11-24 01:12:04', action: 'OCR Indexed: ezpass_ledger_0314.pdf', hash: '8f4e...91a2', tone: 'info' },
    { ts: '2026-11-24 01:14:29', action: 'Conflict Flagged: Impossible Travel (11m)', hash: 'c21b...44f0', tone: 'err' },
    { ts: '2026-11-24 01:18:51', action: 'Audio Transcribed: voicemail_0314_2150.wav', hash: 'a77d...03e8', tone: 'info' },
  ],
};
