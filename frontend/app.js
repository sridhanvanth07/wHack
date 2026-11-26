const $ = s => document.querySelector(s);
let selected = 'facts';
let cachedState = null;
let browserMode = 'iframe';
let chatMessages = [];
let chatSending = false;
const sessionId = (() => {
  let value = localStorage.getItem('carryflow-chat-session');
  if (!value) { value = crypto.randomUUID(); localStorage.setItem('carryflow-chat-session', value); }
  return value;
})();
let savedOpportunities = [];
let preparations = [];
let savedFilter = '';
let sortDeadlines = false;
let resumes = [];
let selectedResumeId = null;
let selectedResume = null;
let currentPreparation = null;
let externalPreparation = false;
const browser = $('#browser');
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, body = {}, method = 'POST') {
  const r = await fetch('/api/' + path, {
    method: method,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    const err = await r.text();
    alert('Operation error: ' + err);
    throw Error(err);
  }
  return r.json();
}

async function load() {
  const s = await (await fetch('/api/state?session_id=' + encodeURIComponent(sessionId))).json();
  cachedState = s;
  render(s);
}

function getActiveFact(facts, conflicts, factId) {
  const resolved = (conflicts || []).find(c => c.fact_id === factId && c.resolved);
  if (resolved && resolved.resolution_version) {
    const match = facts.find(f => f.id === factId && f.version === resolved.resolution_version);
    if (match) return match;
  }
  const nonSup = (facts || []).filter(f => f.id === factId && f.verification !== 'SUPERSEDED');
  if (nonSup.length) {
    return nonSup.sort((a, b) => b.version - a.version)[0];
  }
  return (facts || []).filter(f => f.id === factId).sort((a, b) => b.version - a.version)[0];
}

function render(s) {
  const facts = s.facts || [];
  const rec = s.receipts || [];
  const receiptIds = new Set(rec.map(r=>r.id));
  const prepReceipts = (s.preparations || []).filter(p=>p.receipt && !receiptIds.has(p.receipt.id)).map(p=>({
    id:p.receipt.id, origin:p.receipt.origin, source_field:'Application preparation',
    destination_field:`${p.title} · ${p.receipt.successfully_filled_fields.length} prepared`,
    semantic_type:'application.preparation', mapping_type:'USER_APPROVED_PREPARATION',
    reason:`${p.receipt.approved_mapping_count} approved mapping(s); ${p.receipt.skipped_fields.length} skipped; ${Object.keys(p.receipt.failed_fields).length} failed. No submission occurred.`,
    confidence:1, permission:'RECHECKED', status:p.receipt.recovery_state, created_at:p.receipt.prepared_at,
  }));
  const actions = s.actions || [];
  const events = s.events || [];
  const conflicts = s.conflicts || [];
  $('#saved-count').textContent = String(savedOpportunities.length);

  const uniqueFactIds = [...new Set(facts.map(x => x.id))];
  const activeFacts = uniqueFactIds.map(id => getActiveFact(facts, conflicts, id)).filter(Boolean);
  $('#fact-count').textContent = activeFacts.length;
  $('#receipt-count').textContent = rec.length + prepReceipts.length;
  $('#workflow-id').textContent = s.meta?.workflow_id ? 'Passport · ' + s.meta.workflow_id.slice(0, 8) : 'Workflow not started';

  const pCtrl = $('#passport-controls');
  if (pCtrl) pCtrl.style.display = selected === 'facts' ? 'flex' : 'none';
  $('#chat-compose').style.display = selected === 'chat' ? 'flex' : 'none';
  $('#conflict-box').style.display = ['chat', 'opportunities'].includes(selected) ? 'none' : '';
  document.querySelector('.submit-box').style.display = ['chat', 'opportunities', 'resumes'].includes(selected) ? 'none' : '';

  const recovered = events.some(e => e.title === 'Page inspected before resume');
  const portalA = rec.some(r => r.origin.endsWith('/portal/a'));
  const portalB = rec.some(r => r.origin.endsWith('/portal/b'));
  const profile = events.some(e => e.title === 'Profile facts verified');
  const steps = [
    ['Goal captured', Boolean(s.meta?.user_goal)],
    ['Profile facts verified', profile],
    ['Portal A prepared', portalA],
    ['Portal B prepared', portalB],
    [recovered ? 'Recovery reconciled' : 'Recovery checkpoint', recovered]
  ];
  $('#timeline').innerHTML = steps.map(([label, done], i) =>
    `<div class="tl-item ${done ? 'done' : ''}"><strong>${label}</strong>${i === 4 ? '<small>' + esc(recovered ? 'RECONCILED' : s.meta?.status || 'PENDING') + '</small>' : ''}</div>`
  ).join('');
  $('#progress-count').textContent = `${steps.filter(x => x[1]).length} / 5`;

  $('#inspector').innerHTML = selected === 'facts' ? renderFacts(facts, conflicts) : selected === 'receipts' ? renderReceipts([...rec, ...prepReceipts]) : selected === 'actions' ? renderActions(actions) : selected === 'opportunities' ? (currentPreparation ? renderPreparation(currentPreparation) : renderSavedOpportunities()) : selected === 'resumes' ? renderResumeManager() : renderChat();
  $('#conflict-box').innerHTML = renderConflicts(s, facts);
}

function renderChat() {
  const workflows = cachedState?.agent_workflows || [];
  const workflowPanel = workflows.length ? `<section class="workflow-activity"><h3>Career workflow activity</h3>${workflows.slice(0,3).map(w=>`<article class="workflow-card"><div class="workflow-card-head"><strong>${esc(w.goal)}</strong><span class="status ${w.status==='FAILED'?'conflict':''}">${esc(w.status.replaceAll('_',' '))}</span></div><p>${esc(w.pending_action || w.completion_summary || w.recovery_state || 'CarryFlow is ready for your next instruction.')}</p><small>${w.checkpoints?.length || 0} saved checkpoints · updated ${esc(new Date(w.last_activity).toLocaleString())}</small>${w.decisions?.map(d=>`<div class="workflow-decision"><label for="decision-${esc(d.id)}">${esc(d.prompt)}</label><div><input id="decision-${esc(d.id)}" data-workflow-decision-input="${esc(d.id)}" maxlength="500"><button data-workflow-respond="${esc(d.id)}" data-workflow-id="${esc(w.id)}">Save decision</button></div></div>`).join('') || ''}<div class="workflow-card-actions">${w.status==='PAUSED'?`<button data-workflow-action="resume" data-workflow-id="${esc(w.id)}">Resume</button>`:`<button data-workflow-action="pause" data-workflow-id="${esc(w.id)}" ${['COMPLETED','CANCELLED','FAILED'].includes(w.status)?'disabled':''}>Pause</button>`}<button data-workflow-action="cancel" data-workflow-id="${esc(w.id)}" ${['COMPLETED','CANCELLED'].includes(w.status)?'disabled':''}>Cancel</button></div>${w.activity?.slice(0,4).map(a=>`<div class="workflow-activity-row">${esc(a.summary)} <small>${esc(new Date(a.created_at).toLocaleString())}</small></div>`).join('')}</article>`).join('')}</section>` : '';
  if (!chatMessages.length) {
    return workflowPanel + '<div class="chat-empty"><span>✳</span><strong>How can I help?</strong><p>Ask CarryFlow to find internships, manage saved opportunities, or prepare an application. Goals and checkpoints are saved here.</p></div>';
  }
  return workflowPanel + `<div class="chat-messages" aria-live="polite">${chatMessages.map(message => {
    const stateLabel = message.pending ? 'Working…' : message.toolStatus === 'completed' ? 'Request complete' : message.toolStatus === 'error' ? 'Could not complete' : '';
    return `<article class="chat-message ${message.role === 'user' ? 'user' : 'assistant'}">
      <div class="chat-message-label">${message.role === 'user' ? 'You' : 'CarryFlow'}${stateLabel ? `<span>${esc(stateLabel)}</span>` : ''}</div>
      <p>${esc(message.text)}</p>
      ${message.opportunities?.length ? renderOpportunityCards(message.opportunities) : ''}
      ${message.savedOpportunities?.length ? renderChatSavedCards(message.savedOpportunities) : ''}
      ${message.resumeItems?.length ? renderResumeChatItems(message.resumeItems) : ''}
      ${message.resumeComparisons?.length ? renderResumeComparisons(message.resumeComparisons) : ''}
      ${message.resumes?.length ? `<div class="resume-chat-list">${message.resumes.map(r=>`<div>${esc(r.filename)} · ${r.item_count} extracted · ${r.verified_count} in Passport</div>`).join('')}</div>` : ''}
      ${message.history?.length ? `<div class="chat-history">${message.history.map(h => `<div>${esc(h.previous_status || 'NEW')} → ${esc(h.new_status)} · ${esc(h.title || 'Opportunity')} · ${esc(new Date(h.changed_at).toLocaleString())}</div>`).join('')}</div>` : ''}
      ${message.applicationPreparation ? renderApplicationPreparation(message.applicationPreparation) : ''}
      ${message.sources?.length ? renderSourceStatuses(message.sources, message.retrievedAt) : ''}
    </article>`;
  }).join('')}</div>`;
}

function renderApplicationPreparation(preparation) {
  const receipt = preparation.receipt;
  if (!receipt) return `<div class="resume-chat-list"><article><strong>${esc(preparation.title)}</strong><span class="status">${esc(preparation.status.replaceAll('_',' '))}</span><p>No field preparation receipt is available yet.</p></article></div>`;
  return `<div class="resume-chat-list"><article><strong>${esc(preparation.title)}</strong><p>Prepared: ${receipt.successfully_filled_fields.map(esc).join(', ') || 'none'}</p><p>Skipped ${receipt.skipped_fields.length} · permission blocked ${receipt.permission_blocked_fields.length} · ambiguous ${receipt.ambiguous_fields.length} · failed ${Object.keys(receipt.failed_fields).length}</p><small>No profile values are stored in the receipt.</small></article></div>`;
}

function renderChatSavedCards(records) {
  return `<div class="saved-list chat-saved-list">${records.map(record => {
    const item = record.opportunity;
    const href = safeListingHref(item.listing_url);
    return `<article class="saved-card"><h3>${esc(item.title)}</h3><strong>${esc(item.company_name || 'Company not listed')}</strong><div class="saved-links">${esc(item.location || 'Location not listed')} · ${esc(record.status)}</div>${item.application_deadline?`<div class="saved-links">Deadline ${esc(item.application_deadline)} · source listed</div>`:''}${href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">View ${esc(item.source_name)} listing</a>`:''}${record.notes?.length?`<p>${record.notes.map(n=>esc(n.note)).join('<br>')}</p>`:''}</article>`;
  }).join('')}</div>`;
}

function renderResumeChatItems(items) {
  return `<div class="resume-chat-list">${items.map(item=>`<article><strong>${esc(item.label)}</strong><span class="status ${item.review_status==='USER_VERIFIED'?'':'unconfirmed'}">${esc(item.review_status)}</span><p>${esc(item.value)}</p><small>${esc(item.section)} · extraction confidence ${Math.round(item.confidence*100)}%</small><blockquote>${esc(item.evidence)}</blockquote></article>`).join('')}</div>`;
}

function renderResumeComparisons(items) {
  const label={matches:'Matches Passport',different:'Differs from Passport',not_in_passport:'Not in Passport',conflict_pending:'Conflict pending'};
  return `<div class="resume-chat-list">${items.map(row=>`<article><strong>${esc(row.item.label)}</strong><span class="status ${row.comparison==='different'||row.comparison==='conflict_pending'?'conflict':''}">${esc(label[row.comparison]||row.comparison)}</span><p>Resume: ${esc(row.item.value)}</p>${row.passport_value?`<small>Passport v${row.passport_version}: ${esc(row.passport_value)}</small>`:''}</article>`).join('')}</div>`;
}

function safeListingHref(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' ? parsed.href : null;
  } catch { return null; }
}

function renderSourceStatuses(sources, retrievedAt) {
  const timestamp = retrievedAt ? new Date(retrievedAt).toLocaleString() : '';
  return `<div class="discovery-sources"><strong>Sources checked</strong>${sources.map(source =>
    `<div><span>${esc(source.source_name)}</span><span class="source-state ${esc(source.status)}">${esc(source.status.replaceAll('_', ' '))}</span><small>${esc(source.message)}</small></div>`
  ).join('')}${timestamp ? `<small>Checked ${esc(timestamp)}</small>` : ''}</div>`;
}

function renderOpportunityCards(opportunities) {
  return `<div class="opportunity-list">${opportunities.map(item => {
    const fields = [];
    if (item.location) fields.push(`<span>${esc(item.location)}</span>`);
    if (item.work_mode && item.work_mode !== 'unknown') fields.push(`<span>${esc(item.work_mode)}</span>`);
    else fields.push('<span>Work mode not specified</span>');
    if (item.compensation_text) fields.push(`<span>${esc(item.compensation_text)}${item.compensation_estimated ? ' · estimated' : ''}</span>`);
    else fields.push('<span>Compensation not disclosed</span>');
    if (item.duration) fields.push(`<span>${esc(item.duration)}</span>`);
    if (item.posted_date) fields.push(`<span>Posted ${esc(item.posted_date)}</span>`);
    const skillTags = (item.skills || []).slice(0, 8).map(skill => `<span class="opportunity-tag">${esc(skill)}</span>`).join('');
    const attribution = (item.source_attributions || []).map(source => {
      const href = safeListingHref(source.listing_url);
      if (!href) return '';
      const brand = source.source_name === 'Adzuna' ? '<a class="adzuna-attribution" href="https://www.adzuna.in/" target="_blank" rel="noopener noreferrer">Adzuna</a>' : '';
      return `<span>${brand}<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">View ${esc(source.source_name)} listing</a></span>`;
    }).filter(Boolean).join(' · ');
    const reasons = (item.match_reasons || []).map(reason => `<li>${esc(reason)}</li>`).join('');
    const eligibility = item.eligibility_evidence ? `<p class="eligibility-note">Student-related source text: ${esc(item.eligibility_evidence)} <strong>Verify eligibility on the listing.</strong></p>` : '';
    return `<section class="opportunity-card">
      <h3>${esc(item.title)}</h3>
      <div class="opportunity-company">${esc(item.company_name || 'Company not listed')}</div>
      <div class="opportunity-facts">${fields.join('')}</div>
      ${skillTags ? `<div class="opportunity-tags">${skillTags}</div>` : ''}
      ${item.description ? `<p class="opportunity-description">${esc(item.description.slice(0, 420))}${item.description.length > 420 ? '…' : ''}</p>` : ''}
      ${eligibility}
      ${reasons ? `<div class="match-explanation"><strong>Why it matched</strong><ul>${reasons}</ul></div>` : ''}
      <div class="opportunity-source">${attribution || esc(item.source_name)}${item.retrieved_at ? `<small>Retrieved ${esc(new Date(item.retrieved_at).toLocaleString())}</small>` : ''}</div>
      <button class="small-action accent save-result" data-save-opportunity="${esc(item.id)}">Save opportunity</button>
    </section>`;
  }).join('')}</div>`;
}

const applicationStatuses = ['SAVED','PREPARING','APPLIED','INTERVIEW','OFFER','REJECTED','WITHDRAWN','EXPIRED'];

function renderSavedOpportunities() {
  const filters = ['','SAVED','PREPARING','APPLIED','INTERVIEW','OFFER','REJECTED','WITHDRAWN','EXPIRED'];
  const controls = `<div class="saved-controls"><label>Status <select id="saved-filter">${filters.map(s => `<option value="${s}" ${savedFilter===s?'selected':''}>${s || 'All'}</option>`).join('')}</select></label><label class="deadline-sort"><input id="deadline-sort" type="checkbox" ${sortDeadlines?'checked':''}> Sort by deadline</label></div>`;
  const prior = preparations.length ? `<section class="prep-history"><h3>Application preparation</h3>${preparations.map(p=>`<button data-resume-prep="${esc(p.id)}"><b>${esc(p.title)}</b><small>${esc(p.status.replaceAll('_',' '))} · ${esc(p.origin)}</small></button>`).join('')}</section>` : '';
  if (!savedOpportunities.length) return `${controls}${prior}<div class="empty">No saved opportunities yet. Save a listing from Chat or ask CarryFlow to save a recent result.</div>`;
  return `${controls}${prior}<div class="saved-list">${savedOpportunities.map(record => {
    const item = record.opportunity;
    const deadline = item.application_deadline ? `<span>Deadline ${esc(item.application_deadline)} · source listed</span>` : '<span>Deadline not provided</span>';
    const pay = item.compensation_text ? `<span>${esc(item.compensation_text)}${item.compensation_estimated ? ' · estimated' : ''}</span>` : '';
    const sources = (item.source_attributions || []).map(source => { const href=safeListingHref(source.listing_url); return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(source.source_name)} listing</a>` : ''; }).filter(Boolean).join(' · ');
    const statusOptions = applicationStatuses.map(s => `<option value="${s}" ${record.status===s?'selected':''}>${s}</option>`).join('');
    const notes = (record.notes || []).map(note => `<div class="saved-note"><p>${esc(note.note)}</p><small>${esc(new Date(note.updated_at).toLocaleString())}</small><button data-edit-note="${esc(note.id)}" data-opportunity-id="${esc(record.id)}">Edit</button><button data-delete-note="${esc(note.id)}" data-opportunity-id="${esc(record.id)}">Delete</button></div>`).join('');
    return `<article class="saved-card"><div class="saved-card-title"><div><h3>${esc(item.title)}</h3><strong>${esc(item.company_name || 'Company not listed')}</strong></div><button class="remove-saved" data-remove-saved="${esc(record.id)}" aria-label="Remove saved opportunity">Remove</button></div><div class="saved-facts">${item.location?`<span>${esc(item.location)}</span>`:''}${item.work_mode && item.work_mode!=='unknown'?`<span>${esc(item.work_mode)}</span>`:''}${pay}${deadline}</div><div class="saved-links">${sources || esc(item.source_name)} · Saved ${esc(new Date(record.saved_at).toLocaleDateString())}</div><button class="small-action accent prep-start" data-prepare-opportunity="${esc(record.id)}">Prepare application</button><label class="status-select">Application status <select data-status-opportunity="${esc(record.id)}">${statusOptions}</select></label>${record.applied_at?`<small class="applied-date">Applied ${esc(new Date(record.applied_at).toLocaleDateString())}</small>`:''}<div class="saved-notes-list">${notes}</div><div class="note-add"><input maxlength="2000" placeholder="Add a private note" data-note-input="${esc(record.id)}"><button data-add-note="${esc(record.id)}">Add note</button></div></article>`;
  }).join('')}</div>`;
}

async function loadSavedOpportunities() {
  const params = new URLSearchParams();
  if (savedFilter) params.set('status', savedFilter);
  if (sortDeadlines) params.set('sort_by_deadline', 'true');
  const response = await fetch('/api/opportunities/saved' + (params.size ? '?' + params : ''));
  if (!response.ok) throw new Error('Could not load saved opportunities');
  savedOpportunities = (await response.json()).opportunities || [];
  const prepResponse = await fetch('/api/preparations');
  preparations = prepResponse.ok ? (await prepResponse.json()).preparations || [] : [];
  $('#saved-count').textContent = String(savedOpportunities.length);
  if (selected === 'opportunities') $('#inspector').innerHTML = currentPreparation ? renderPreparation(currentPreparation) : renderSavedOpportunities();
}

function renderPreparation(prep) {
  const reviewOpen = prep.status === 'REVIEW_PENDING';
  const activeFacts = [...new Map((cachedState?.facts || []).filter(f=>f.verification==='USER_CONFIRMED' && f.user_confirmed).map(f=>[f.id,getActiveFact(cachedState.facts,cachedState.conflicts,f.id)])).values()].filter(Boolean);
  const fields = prep.fields.map(field => {
    const eligible = activeFacts.filter(f => (field.semantic_type==='unknown'||field.semantic_type==='address.unknown'||f.semantic_type===field.semantic_type) && JSON.parse(f.permitted_origins||'[]').includes(prep.origin));
    const select = reviewOpen && ['AMBIGUOUS','PROPOSED','APPROVED'].includes(field.mapping_status) && eligible.length ? `<label class="prep-map-label">Choose Passport fact<select data-prep-map="${esc(field.field_id)}"><option value="">Choose a fact…</option>${eligible.map(f=>`<option value="${esc(f.id)}" ${field.fact_id===f.id?'selected':''}>${esc(f.source_label||f.id)} · ${esc(f.value)}</option>`).join('')}</select></label>` : '';
    const actions = reviewOpen && ['PROPOSED','AMBIGUOUS','MISSING_FACT','PERMISSION_BLOCKED','UNSUPPORTED','APPROVED'].includes(field.mapping_status) ? `<div class="prep-field-actions">${field.mapping_status==='PROPOSED'?`<button data-prep-review="approve" data-field-id="${esc(field.field_id)}">Approve mapping</button>`:''}${field.mapping_status==='PERMISSION_BLOCKED' && field.fact_id?`<button data-prep-share="${esc(field.fact_id)}" data-share-origin="${esc(prep.origin)}">Allow this destination for this fact</button>`:''}${!['UNSUPPORTED'].includes(field.mapping_status)?`<button data-prep-review="blank" data-field-id="${esc(field.field_id)}">Leave blank</button><button data-prep-review="reject" data-field-id="${esc(field.field_id)}">Reject</button>`:''}</div>` : '';
    return `<article class="prep-field"><div class="prep-field-head"><strong>${esc(field.label || 'Unlabeled field')}</strong><span class="status ${['AMBIGUOUS','PERMISSION_BLOCKED','MISSING_FACT'].includes(field.mapping_status)?'conflict':''}">${esc(field.mapping_status.replaceAll('_',' '))}</span></div><small>${esc(field.field_type)}${field.required?' · required':''}${field.visible?'':' · hidden'} · mapping confidence ${Math.round(field.mapping_confidence*100)}%</small>${field.description?`<small>${esc(field.description)}</small>`:''}${field.options?.length?`<small>Choices: ${field.options.map(esc).join(' · ')}</small>`:''}${field.fact_label?`<div class="prep-value"><b>${esc(field.fact_label)}:</b> ${esc(field.value_preview)}</div>`:''}<p>${esc(field.explanation)}</p>${select}${actions}</article>`;
  }).join('');
  const receipt = prep.receipt ? `<section class="prep-receipt"><h4>Handoff receipt · ${esc(prep.receipt.recovery_state)}</h4><p>Filled ${prep.receipt.successfully_filled_fields.length} of ${prep.receipt.approved_mapping_count} approved field(s). No values are stored in this receipt.</p>${prep.receipt.successfully_filled_fields.map(x=>`<small>Filled · ${esc(x)}</small>`).join('')}${Object.entries(prep.receipt.failed_fields||{}).map(([k,v])=>`<small class="prep-error">Could not fill · ${esc(k)}: ${esc(v)}</small>`).join('')}${prep.receipt.skipped_fields.map(x=>`<small>Skipped · ${esc(x)}</small>`).join('')}</section>` : '';
  const resolved = prep.fields.every(f=>['APPROVED','REJECTED','BLANK','UNSUPPORTED'].includes(f.mapping_status));
  return `<button class="prep-back" data-prep-back>← Saved opportunities</button><section class="prep-header"><h3>${esc(prep.title)}</h3><strong>${esc(prep.company_name||'Company not listed')}</strong><p>Destination: <b>${esc(prep.origin)}</b></p><a href="${esc(prep.destination_url)}" target="_blank" rel="noopener noreferrer">Application page</a><small>Status: ${esc(prep.status.replaceAll('_',' '))} · browser access remains restricted to this approved origin</small></section><p class="prep-safety">Review every proposed mapping. Passport sharing permission and your approval are both required. Fields are prepared only; CarryFlow never submits.</p>${fields || '<div class="empty">No safe form fields were found.</div>'}${resolved && prep.status==='REVIEW_PENDING'?'<button class="primary prep-plan-approve" data-prep-approve-plan>Approve reviewed plan</button>':''}${prep.status==='PREPARATION_APPROVED'?'<button class="primary prep-plan-approve" data-prep-fill>Fill approved fields</button>':''}${['REVIEW_PENDING','PREPARATION_APPROVED','PREPARED','PARTIALLY_PREPARED','FAILED'].includes(prep.status)?'<button class="secondary prep-plan-approve" data-prep-recover>Reopen and inspect again</button>':''}${receipt}`;
}

async function beginApplicationPreparation(opportunityId) {
  const response = await fetch(`/api/preparations/preview/${encodeURIComponent(opportunityId)}`);
  if (!response.ok) { alert('This saved opportunity does not have a safe, reachable application destination.'); return; }
  const preview = await response.json();
  if (!preview.origin) throw new Error('Could not validate this application destination.');
  const message = `${preview.title}${preview.company_name ? ` at ${preview.company_name}` : ''}\n\nApplication URL: ${preview.destination_url}\nDestination origin: ${preview.origin}\n\nCarryFlow will open this origin in its controlled browser and inspect visible form fields. The site origin and Passport sharing permissions are separate approvals. No application will be submitted. Continue?`;
  if (!confirm(message)) return;
  if (!preview.origin_approved) await api('preparations/origins/approve', {origin: preview.origin});
  externalPreparation = true;
  setMode('playwright');
  const result = await api('preparations/start', {opportunity_id: opportunityId});
  currentPreparation = result.preparation;
  $('#site-name').textContent = currentPreparation.company_name || currentPreparation.title;
  $('#site-url').textContent = currentPreparation.origin;
  $('#address').textContent = new URL(currentPreparation.destination_url).host + new URL(currentPreparation.destination_url).pathname;
  selected = 'opportunities';
  $('#inspector').innerHTML = renderPreparation(currentPreparation);
  await updateScreenshot();
}

async function updatePreparation(result) {
  currentPreparation = result.preparation || result;
  $('#browser-state').textContent = `Application ${currentPreparation.status.toLowerCase().replaceAll('_',' ')} · no submission`;
  await load();
  $('#inspector').innerHTML = renderPreparation(currentPreparation);
}

async function saveSearchOpportunity(opportunityId) {
  await api('opportunities/save', {session_id: sessionId, opportunity_id: opportunityId});
  await loadSavedOpportunities();
}

function renderResumeManager() {
  const upload = `<form id="resume-upload" class="resume-upload"><label for="resume-file">Add a resume</label><input id="resume-file" name="file" type="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" required><small>PDF or DOCX · up to 8 MB · original file is processed in memory, then discarded</small><button class="small-action accent" type="submit">Upload and extract</button></form>`;
  const list = resumes.length ? `<div class="resume-list">${resumes.map(resume => `<div class="resume-row ${resume.id===selectedResumeId?'active':''}"><button data-select-resume="${esc(resume.id)}"><b>${esc(resume.filename)}</b><small>${esc(resume.file_type)} · ${resume.item_count} items · ${resume.verified_count} verified</small></button><button class="remove-resume" data-delete-resume="${esc(resume.id)}" aria-label="Remove resume">×</button></div>`).join('')}</div>` : '<div class="empty">No resume has been added yet.</div>';
  if (!selectedResume) return `${upload}${list}<p class="resume-privacy">Extracted details are not verified until you review and accept them. Scanned PDFs need OCR, which is not enabled.</p>`;
  const grouped = {};
  selectedResume.items.forEach(item => (grouped[item.category] ||= []).push(item));
  const statusLabel = {EXTRACTED:'Needs review', EDITED:'Edited · needs review', USER_VERIFIED:'Added to Passport', REJECTED:'Rejected', CONFLICT_PENDING:'Passport conflict · resolve below'};
  const isPending = item => ['EXTRACTED','EDITED'].includes(item.review_status);
  const items = Object.entries(grouped).map(([category, entries]) => `<section class="resume-section"><h4>${esc(category.replaceAll('_',' '))}</h4>${entries.map(item => `<article class="resume-item ${isPending(item)?'unreviewed':''}"><div class="resume-item-top">${isPending(item)?`<input type="checkbox" data-review-select="${esc(item.id)}" aria-label="Select ${esc(item.label)}">`:''}<strong>${esc(item.label)}</strong><span class="status ${item.review_status==='CONFLICT_PENDING'?'conflict':item.review_status==='USER_VERIFIED'?'':'unconfirmed'}">${esc(statusLabel[item.review_status] || item.review_status)}</span></div><div class="resume-item-value">${esc(item.value)}</div>${item.original_value && item.original_value!==item.value?`<small>Extracted value: ${esc(item.original_value)}</small>`:''}<small>Section: ${esc(item.section)} · extraction confidence ${Math.round(item.confidence*100)}%</small><blockquote>${esc(item.evidence)}</blockquote>${item.details && Object.keys(item.details).length?`<details><summary>Structured details</summary><pre>${esc(JSON.stringify(item.details,null,2))}</pre></details>`:''}${item.review_error?`<p class="resume-error">${esc(item.review_error)}</p>`:''}${isPending(item)?`<div class="resume-item-actions"><button data-accept-resume-item="${esc(item.id)}">Accept</button><button data-edit-resume-item="${esc(item.id)}">Edit</button><button data-reject-resume-item="${esc(item.id)}">Reject</button></div>`:''}</article>`).join('')}</section>`).join('');
  const pending = selectedResume.items.some(isPending);
  return `${upload}${list}<div class="resume-selected"><strong>${esc(selectedResume.resume.filename)}</strong><small>Uploaded ${esc(new Date(selectedResume.resume.uploaded_at).toLocaleString())} · ${esc(selectedResume.resume.extraction_status)}</small>${selectedResume.resume.warning?`<p class="resume-warning">${esc(selectedResume.resume.warning)}</p>`:''}<p class="resume-privacy">Review items individually. Accepted items are marked user verified. Conflicting Passport values stay pending until you resolve the existing conflict card.</p>${pending?`<div class="resume-bulk"><button class="small-action accent" data-approve-selected>Accept selected</button><button class="small-action" data-reject-selected>Reject selected</button></div>`:''}${items || '<div class="empty">No reviewable information was found.</div>'}</div>`;
}

async function loadResumes(selectId = selectedResumeId) {
  const listResponse = await fetch('/api/resumes');
  if (!listResponse.ok) throw new Error('Could not load resumes');
  resumes = (await listResponse.json()).resumes || [];
  selectedResumeId = resumes.some(item => item.id === selectId) ? selectId : resumes[0]?.id || null;
  selectedResume = null;
  if (selectedResumeId) {
    const response = await fetch('/api/resumes/' + encodeURIComponent(selectedResumeId));
    if (response.ok) selectedResume = await response.json();
  }
  if (selected === 'resumes') $('#inspector').innerHTML = renderResumeManager();
}

async function uploadResume(form) {
  const file = form.querySelector('input[type=file]')?.files?.[0];
  if (!file) return;
  const data = new FormData();
  data.append('file', file);
  const button = form.querySelector('button[type=submit]');
  button.disabled = true;
  button.textContent = 'Extracting…';
  try {
    const response = await fetch('/api/resumes/upload', {method:'POST', body:data});
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Resume extraction failed');
    selectedResumeId = body.resume.id;
    await loadResumes(selectedResumeId);
    await load();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; button.textContent = 'Upload and extract'; }
}

async function reviewResumeItems(action, itemIds) {
  if (!selectedResumeId || !itemIds.length) return;
  await api(`resumes/${encodeURIComponent(selectedResumeId)}/${action}`, {item_ids:itemIds});
  await loadResumes(selectedResumeId);
  await load();
}

async function sendChatMessage(event) {
  event.preventDefault();
  if (chatSending) return;
  const input = $('#chat-message');
  const message = input.value.trim();
  if (!message) return;

  chatSending = true;
  $('#chat-send').disabled = true;
  input.disabled = true;
  $('#chat-progress').textContent = 'Working on your request…';
  chatMessages.push({role: 'user', text: message});
  const pending = {role: 'assistant', text: 'I’m checking the workflow now.', pending: true};
  chatMessages.push(pending);
  input.value = '';
  render(cachedState || {});
  $('#inspector').scrollTop = $('#inspector').scrollHeight;

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message, session_id: sessionId})
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const result = await response.json();
    pending.text = result.assistant_message || 'I couldn’t complete that request.';
    pending.pending = false;
    pending.toolStatus = result.tool_status;
    if (result.tool?.tool_name === 'search_internships') {
      pending.opportunities = result.tool.data?.opportunities || [];
      pending.sources = result.tool.data?.sources || [];
      pending.retrievedAt = result.tool.data?.retrieved_at || null;
    }
    if (result.tool?.tool_name === 'list_saved_opportunities') pending.savedOpportunities = result.tool.data?.opportunities || [];
    if (result.tool?.tool_name === 'get_application_history') pending.history = result.tool.data?.history || [];
    if (result.tool?.tool_name === 'list_resumes') pending.resumes = result.tool.data?.resumes || [];
    if (result.tool?.tool_name === 'get_resume_extraction') pending.resumeItems = result.tool.data?.items || [];
    if (result.tool?.tool_name === 'compare_resume') pending.resumeComparisons = result.tool.data?.comparisons || [];
    if (result.tool?.tool_name === 'get_application_preparation') pending.applicationPreparation = result.tool.data?.preparation || null;
    if (result.tool_status === 'completed') {
      try { await load(); await loadSavedOpportunities(); } catch { render(cachedState || {}); }
    } else {
      render(cachedState || {});
    }
  } catch {
    pending.text = 'I couldn’t complete that request. Please try again.';
    pending.pending = false;
    pending.toolStatus = 'error';
    render(cachedState || {});
  } finally {
    chatSending = false;
    $('#chat-send').disabled = false;
    input.disabled = false;
    $('#chat-progress').textContent = '';
    input.focus();
    $('#inspector').scrollTop = $('#inspector').scrollHeight;
  }
}

function renderFacts(facts, conflicts) {
  const ids = [...new Set(facts.map(f => f.id))];
  const activeList = ids.map(id => getActiveFact(facts, conflicts, id)).filter(Boolean);
  if (!activeList.length) return '<div class="empty">Your personal data passport is empty. Add a fact or start the demo to initialize.</div>';

  return activeList.map(f => {
    let origins = [];
    try { origins = JSON.parse(f.permitted_origins || '[]'); } catch { origins = []; }
    const originsFormatted = origins.map(o => o.replace(/https?:\/\//, '')).join(', ') || 'None';
    const isConfirmed = Boolean(f.user_confirmed);
    const isConflict = f.verification === 'UNVERIFIED_CONFLICT';
    const statusClass = isConflict ? 'conflict' : isConfirmed ? '' : 'unconfirmed';
    const statusText = isConflict ? '⚡ Conflict' : isConfirmed ? '✓ Confirmed' : '⚠ Unconfirmed';
    let displayValue = f.value;
    if (['resume','resume_edited'].includes(f.source_type) && String(f.semantic_type || '').startsWith('career.')) {
      try {
        const structured = JSON.parse(f.value);
        displayValue = structured.summary || f.value;
        if (structured.details?.technologies_as_written?.length) displayValue += `\nTechnologies: ${structured.details.technologies_as_written.join(', ')}`;
        if (structured.details?.dates_as_written) displayValue += `\nDates as written: ${structured.details.dates_as_written}`;
      } catch {}
    }

    return `<div class="fact" data-fact-id="${esc(f.id)}">
      <div class="fact-top">
        <b>${esc(f.source_label || f.id)}</b>
        <span class="status ${statusClass}">${statusText}</span>
      </div>
      <div class="fact-value">${esc(displayValue)}</div>
      <div class="fact-provenance">
        <span>Source: <b>${esc(f.source_url.split('/').pop() || f.source_url)}</b> · v${f.version}</span><br>
        ${['resume','resume_edited'].includes(f.source_type) ? `<span class="resume-source">${f.source_type==='resume_edited'?'Resume · edited by you':'Resume'} · ${esc(f.source_section || 'section')} · extraction quality ${Math.round((f.extraction_confidence ?? f.confidence)*100)}%</span><br>${f.source_evidence?`<span class="resume-evidence">Evidence: ${esc(f.source_evidence)}</span><br>`:''}` : f.source_type === 'manual_edit' ? '<span>Source type: manually edited</span><br>' : ''}
        <span>Added: ${esc(f.collected_at ? f.collected_at.slice(0, 16).replace('T', ' ') : 'N/A')}</span> · 
        <span>Confirmed: ${esc(f.last_confirmed_at ? f.last_confirmed_at.slice(0, 16).replace('T', ' ') : 'Pending')}</span>
      </div>
      <div class="fact-perms">Sharing: <b>${esc(originsFormatted)}</b></div>
      <div class="fact-actions">
        ${!isConfirmed ? `<button type="button" class="tiny-btn confirm" onclick="confirmFact('${esc(f.id)}', ${f.version})">Confirm</button>` : ''}
        <button type="button" class="tiny-btn" onclick="openEditFactModal('${esc(f.id)}')">Edit</button>
        <button type="button" class="tiny-btn" onclick="openPermissionsModal('${esc(f.id)}')">Sharing</button>
      </div>
    </div>`;
  }).join('');
}

const CANONICAL_MAP = {
  'person.name': 'full_name',
  'person.email': 'email',
  'person.phone': 'phone',
  'education.degree': 'degree',
  'education.graduation_year': 'graduation_year',
  'skills.technical': 'skills',
  'address.current': 'current_address',
  'address.permanent': 'permanent_address'
};

function renderReceipts(rs) {
  if (!rs.length) return '<div class="empty">Prepare a portal to create inspectable handoff receipts.</div>';
  return rs.map(r => {
    const canon = CANONICAL_MAP[r.semantic_type] || (r.mapping_type === 'AMBIGUOUS' ? 'unresolved (ambiguous)' : r.mapping_type === 'UNCONFIRMED_FACT' ? 'unconfirmed profile fact' : 'unmapped');
    return `<div class="receipt">
      <div class="receipt-title"><span>${esc(r.source_field || 'Unresolved source')} → ${esc(r.destination_field.replaceAll('_', ' '))}</span><span class="status ${r.status === 'WAITING_FOR_USER' ? 'conflict' : ''}">${esc(r.status)}</span></div>
      <p>${esc(r.reason)}</p>
      <div class="receipt-meta"><span><b>Canonical:</b> ${esc(canon)}</span> · <span>${esc(r.mapping_type)}</span> · <span>confidence ${Math.round(r.confidence * 100)}%</span> · <span>permission ${esc(r.permission)}</span></div>
    </div>`;
  }).join('');
}

function renderActions(as) {
  if (!as.length) return '<div class="empty">No action has run. Prepared and verified actions will appear here.</div>';
  return as.map(a =>
    `<div class="receipt">
      <div class="receipt-title"><span>${esc(a.action_type.replaceAll('_', ' '))}</span><span class="status ${a.status === 'UNCERTAIN' ? 'conflict' : ''}">${esc(a.status)}</span></div>
      <p>${esc(a.observed_result)}</p>
      <div class="receipt-meta">${esc(a.origin.split('/').pop())} · ${esc(a.verification)}</div>
    </div>`
  ).join('');
}

function renderConflicts(s, facts) {
  const unresolved = (s.conflicts || []).filter(c => !c.resolved);
  if (!unresolved.length) return '';
  return unresolved.map(c => {
    const old = facts.find(f => f.id === c.fact_id && f.version === c.old_version);
    const n = facts.find(f => f.id === c.fact_id && f.version === c.new_version);
    const label = n?.source_label || old?.source_label || c.fact_id.replaceAll('_', ' ');
    return `<div class="conflict-card" data-fact="${esc(c.fact_id)}">
      <h3>⚠ Conflicting ${esc(label)} values</h3>
      <p>v${c.old_version}: ${esc(old?.value)} · ${esc(old?.source_label || 'Original')} (${esc(old?.verification || 'SAVED')})</p>
      <p>v${c.new_version}: ${esc(n?.value)} · ${esc(n?.source_label || 'Incoming')} · ${esc(n?.verification || 'UNVERIFIED')}</p>
      <button type="button" onclick="resolve('${esc(c.fact_id)}', ${c.old_version})">Keep v${c.old_version}</button>
      <button type="button" onclick="resolve('${esc(c.fact_id)}', ${c.new_version})">Use v${c.new_version}</button>
    </div>`;
  }).join('');
}

async function resolve(fact_id, version) {
  await api('resolve', {fact_id, version});
  await load();
  if (selected === 'resumes') await loadResumes(selectedResumeId);
}
window.resolve = resolve;

async function confirmFact(fact_id, version) {
  await api('passport/fact/' + fact_id + '/confirm', {version});
  await load();
}
window.confirmFact = confirmFact;

function closeFactModal() {
  const m = $('#fact-modal');
  m.style.display = 'none';
  m.innerHTML = '';
}
window.closeFactModal = closeFactModal;

function openAddFactModal() {
  const m = $('#fact-modal');
  m.style.display = 'block';
  m.innerHTML = `
    <h3>Add Profile Fact to Passport</h3>
    <label>Field Type</label>
    <select id="modal-fact-id">
      <option value="full_name">Full Name (person.name)</option>
      <option value="email">Email Address (person.email)</option>
      <option value="phone">Phone Number (person.phone)</option>
      <option value="degree">Academic Degree (education.degree)</option>
      <option value="graduation_year">Graduation Year (education.graduation_year)</option>
      <option value="skills">Technical Skills (skills.technical)</option>
      <option value="current_address">Current Address (address.current)</option>
      <option value="permanent_address">Permanent Address (address.permanent)</option>
      <option value="custom">Custom Field...</option>
    </select>
    <div id="custom-fields-group" style="display:none;margin-top:6px;">
      <label>Custom Field Identifier</label>
      <input type="text" id="modal-custom-id" placeholder="e.g. portfolio_url">
      <label>Custom Label</label>
      <input type="text" id="modal-custom-label" placeholder="e.g. Portfolio Website">
    </div>
    <label>Value</label>
    <input type="text" id="modal-fact-val" placeholder="Enter fact value">
    <label>Sharing Permissions</label>
    <div style="font-size:10px;margin-top:4px;">
      <label style="display:inline;font-size:10px;"><input type="checkbox" id="perm-portal-a" checked style="width:auto;"> Allow 127.0.0.1:8000 (Northstar / Summit)</label>
    </div>
    <div style="font-size:10px;margin-top:6px;">
      <label style="display:inline;font-size:10px;"><input type="checkbox" id="modal-confirm-now" checked style="width:auto;"> Confirm this fact immediately</label>
    </div>
    <div class="form-actions">
      <button type="button" class="tiny-btn" onclick="closeFactModal()">Cancel</button>
      <button type="button" class="tiny-btn primary" id="modal-submit-add">Save Fact</button>
    </div>
  `;

  $('#modal-fact-id').onchange = () => {
    $('#custom-fields-group').style.display = $('#modal-fact-id').value === 'custom' ? 'block' : 'none';
  };

  $('#modal-submit-add').onclick = async () => {
    const sel = $('#modal-fact-id').value;
    const isCustom = sel === 'custom';
    const id = isCustom ? $('#modal-custom-id').value.trim() : sel;
    const label = isCustom ? $('#modal-custom-label').value.trim() : $('#modal-fact-id').selectedOptions[0].text.split(' (')[0];
    const val = $('#modal-fact-val').value.trim();

    if (!id || !val) {
      alert('Field and Value are required');
      return;
    }

    const permitted = [];
    if ($('#perm-portal-a').checked) {
      permitted.push('http://127.0.0.1:8000', 'http://localhost:8000');
    }

    const confirmed = $('#modal-confirm-now').checked;

    await api('passport/fact', {
      id: id,
      label: label,
      value: val,
      permitted_origins: permitted,
      user_confirmed: confirmed
    });

    closeFactModal();
    await load();
  };
}
window.openAddFactModal = openAddFactModal;

function openEditFactModal(factId) {
  const facts = cachedState?.facts || [];
  const fact = facts.filter(f => f.id === factId).sort((a, b) => b.version - a.version)[0];
  if (!fact) return;

  const m = $('#fact-modal');
  m.style.display = 'block';
  m.innerHTML = `
    <h3>Edit Fact: ${esc(fact.source_label || fact.id)}</h3>
    <label>Current Value (v${fact.version})</label>
    <input type="text" id="modal-edit-val" value="${esc(fact.value)}">
    <div style="font-size:9px;color:#7b8882;margin-top:4px;">* Changing the value creates a new version (v${fact.version + 1}) to preserve provenance.</div>
    <div style="font-size:10px;margin-top:8px;">
      <label style="display:inline;font-size:10px;"><input type="checkbox" id="modal-edit-confirm" checked style="width:auto;"> Confirm updated version</label>
    </div>
    <div class="form-actions">
      <button type="button" class="tiny-btn" onclick="closeFactModal()">Cancel</button>
      <button type="button" class="tiny-btn primary" id="modal-submit-edit">Save Update</button>
    </div>
  `;

  $('#modal-submit-edit').onclick = async () => {
    const val = $('#modal-edit-val').value.trim();
    if (!val) {
      alert('Value cannot be empty');
      return;
    }
    const confirmed = $('#modal-edit-confirm').checked;

    await api('passport/fact/' + factId, {
      value: val,
      user_confirmed: confirmed,
      source_label: fact.source_label
    }, 'PUT');

    closeFactModal();
    await load();
  };
}
window.openEditFactModal = openEditFactModal;

function openPermissionsModal(factId, suggestedOrigin = null) {
  const facts = cachedState?.facts || [];
  const fact = facts.filter(f => f.id === factId).sort((a, b) => b.version - a.version)[0];
  if (!fact) return;

  let currentPerms = [];
  try { currentPerms = JSON.parse(fact.permitted_origins || '[]'); } catch { currentPerms = []; }
  const localOrigins = ['http://127.0.0.1:8000', 'http://localhost:8000'];
  const isAllowedLocal = currentPerms.some(p => localOrigins.includes(p)) || localOrigins.includes(suggestedOrigin);

  const m = $('#fact-modal');
  m.style.display = 'block';
  m.innerHTML = `
    <h3>Sharing Permissions: ${esc(fact.source_label || fact.id)}</h3>
    <p style="font-size:10px;color:#607069;margin:4px 0 8px;">Select which destination websites are permitted to access this fact.</p>
    <div>
      <label style="display:inline;font-size:10px;"><input type="checkbox" id="modal-perm-local" ${isAllowedLocal ? 'checked' : ''} style="width:auto;"> Approved Local Portals (127.0.0.1:8000 / localhost:8000)</label>
    </div>
    <label for="modal-perm-origins" style="display:block;font-size:10px;margin-top:8px;">Additional exact HTTPS origins (one per line)</label>
    <textarea id="modal-perm-origins" rows="3" placeholder="https://careers.example.com">${esc([...new Set([...currentPerms.filter(p => !localOrigins.includes(p)), ...(suggestedOrigin && !localOrigins.includes(suggestedOrigin) && !currentPerms.includes(suggestedOrigin) ? [suggestedOrigin] : [])])].join('\n'))}</textarea>
    <small style="font-size:8px;color:#77877f;">This grants sharing permission for this fact only. The site origin must also be separately approved before opening.</small>
    <div class="form-actions">
      <button type="button" class="tiny-btn" onclick="closeFactModal()">Cancel</button>
      <button type="button" class="tiny-btn primary" id="modal-submit-perm">Update Permissions</button>
    </div>
  `;

  $('#modal-submit-perm').onclick = async () => {
    const permitted = [];
    if ($('#modal-perm-local').checked) {
      permitted.push('http://127.0.0.1:8000', 'http://localhost:8000');
    }
    permitted.push(...$('#modal-perm-origins').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean));

    await api('passport/fact/' + factId + '/permissions', {
      permitted_origins: permitted
    });

    closeFactModal();
    await load();
  };
}
window.openPermissionsModal = openPermissionsModal;

function currentOrigin() {
  try {
    const u = new URL(browser.src, window.location.href);
    return u.origin + u.pathname;
  } catch {
    return browser.src || '';
  }
}

async function updateScreenshot() {
  try {
    const res = await fetch('/api/automation/screenshot?t=' + Date.now());
    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const img = $('#playwright-screenshot');
      const ph = $('#playwright-placeholder');
      if (img) {
        img.src = url;
        img.style.display = 'block';
      }
      if (ph) ph.style.display = 'none';
    }
  } catch {}
}

function setMode(mode) {
  browserMode = mode;
  const btnIframe = $('#mode-iframe');
  const btnPlaywright = $('#mode-playwright');
  const iframeEl = $('#browser');
  const pwContainer = $('#playwright-container');
  const connState = $('#conn-state');

  if (mode === 'playwright') {
    if (btnPlaywright) btnPlaywright.classList.add('active');
    if (btnIframe) btnIframe.classList.remove('active');
    if (iframeEl) iframeEl.style.display = 'none';
    if (pwContainer) pwContainer.style.display = 'block';
    if (connState) connState.textContent = 'Playwright Chromium connected';
    // Navigate Playwright browser to match current view
    if (externalPreparation) updateScreenshot();
    else {
      const origin = currentOrigin();
      api('automation/navigate', {url: origin}).then(() => updateScreenshot()).catch(() => {});
    }
  } else {
    if (btnIframe) btnIframe.classList.add('active');
    if (btnPlaywright) btnPlaywright.classList.remove('active');
    if (iframeEl) iframeEl.style.display = 'block';
    if (pwContainer) pwContainer.style.display = 'none';
    if (connState) connState.textContent = 'Browser connected';
  }
}

$('#mode-iframe').onclick = () => setMode('iframe');
$('#mode-playwright').onclick = () => setMode('playwright');

function observed() {
  try {
    const doc = browser.contentDocument;
    if (!doc) return {};
    const result = {};
    const elements = doc.querySelectorAll('input, textarea, select');
    for (const el of elements) {
      const name = el.name;
      if (!name) continue;
      const type = (el.type || '').toLowerCase();
      if (type === 'submit' || type === 'button' || type === 'reset' || type === 'image') {
        continue;
      }
      if (type === 'radio') {
        if (el.checked) {
          result[name] = el.value;
        } else if (!(name in result)) {
          result[name] = '';
        }
      } else if (type === 'checkbox') {
        if (el.checked) {
          result[name] = el.value || 'true';
        } else if (!(name in result)) {
          result[name] = '';
        }
      } else if (el.tagName.toLowerCase() === 'select') {
        result[name] = el.value;
      } else {
        result[name] = el.value;
      }
    }
    return result;
  } catch {
    return {};
  }
}

function navigate(url, name) {
  browser.src = url;
  $('#site-name').textContent = name;
  let fullUrl = url;
  try {
    const u = new URL(url, window.location.origin);
    fullUrl = u.origin + u.pathname;
    $('#address').textContent = u.host + u.pathname;
  } catch {
    $('#address').textContent = url.replace('http://', '');
  }
  $('#site-url').textContent = '127.0.0.1 · approved demo origin';
  if (url.endsWith('/source')) {
    $('#browser-state').textContent = 'Source profile · Facts ready to carry forward';
  } else {
    $('#browser-state').textContent = 'Page ready · synthetic data';
  }

  if (browserMode === 'playwright') {
    api('automation/navigate', {url: fullUrl})
      .then(() => updateScreenshot())
      .catch(() => {});
  }
}

$('#start').onclick = async () => {
  await api('start', {user_goal: $('#goal-input').value});
  navigate('/portal/source', 'Student profile');
  await load();
};

$('#reset').onclick = async () => {
  await api('reset');
  navigate('/portal/source', 'Student profile');
  await load();
  if (selected === 'resumes') await loadResumes(selectedResumeId);
};

$('#portal-source').onclick = () => navigate('/portal/source', 'Student profile');
$('#portal-a').onclick = () => navigate('/portal/a', 'Northstar Internships');
$('#portal-b').onclick = () => navigate('/portal/b', 'Summit Talent');

const addBtn = $('#add-fact-btn');
if (addBtn) addBtn.onclick = openAddFactModal;

$('#prepare').onclick = async () => {
  const origin = currentOrigin();
  if (origin.endsWith('/source')) {
    $('#browser-state').textContent = 'Source profile is active. Select Portal A or Portal B to prepare.';
    return;
  }
  let p = null;
  if (origin.endsWith('/a')) p = 'a';
  else if (origin.endsWith('/b')) p = 'b';
  if (!p) {
    $('#browser-state').textContent = 'Please navigate to an approved application portal to prepare.';
    return;
  }
  await api('prepare/' + p);
  $('#browser-state').textContent = `Portal ${p.toUpperCase()} prepared · Review handoff receipts`;
  await load();
};

$('#fill').onclick = async () => {
  if (browserMode === 'playwright') {
    $('#browser-state').textContent = 'Playwright is filling approved fields...';
    try {
      await api('automation/fill');
      await updateScreenshot();
      $('#browser-state').textContent = 'Playwright filled approved fields into Chromium';
    } catch (e) {
      $('#browser-state').textContent = 'Playwright fill error: ' + e.message;
    }
    await load();
    return;
  }

  const s = await (await fetch('/api/state')).json();
  const origin = currentOrigin();
  const pending = s.receipts.filter(r => r.origin === origin && r.status === 'PROPOSED');
  const doc = browser.contentDocument;

  for (const r of pending) {
    if (r.permission !== 'APPROVED') continue;
    const fact = getActiveFact(s.facts, s.conflicts, r.source_fact_id);
    if (!fact || fact.verification === 'UNVERIFIED_CONFLICT' || !fact.user_confirmed) continue;

    const key = `${origin}|${r.destination_field}|${fact.id}|${fact.version}`;
    const prior = s.actions.find(a => a.idempotency_key === key);
    if (prior && ['VERIFIED', 'UNCERTAIN', 'PREPARED'].includes(prior.status)) continue;

    if (doc) {
      const targets = doc.querySelectorAll(`[name="${CSS.escape(r.destination_field)}"]`);
      for (const target of targets) {
        const type = (target.type || '').toLowerCase();
        if (type === 'radio') {
          if (target.value === fact.value) {
            target.checked = true;
            target.dispatchEvent(new Event('input', {bubbles: true}));
            target.dispatchEvent(new Event('change', {bubbles: true}));
          }
        } else if (type === 'checkbox') {
          target.checked = (fact.value === 'true' || fact.value === '1' || fact.value === target.value);
          target.dispatchEvent(new Event('input', {bubbles: true}));
          target.dispatchEvent(new Event('change', {bubbles: true}));
        } else if (target.tagName.toLowerCase() === 'select') {
          target.value = fact.value;
          target.dispatchEvent(new Event('change', {bubbles: true}));
        } else {
          target.value = fact.value;
          target.dispatchEvent(new Event('input', {bubbles: true}));
          target.dispatchEvent(new Event('change', {bubbles: true}));
        }
      }
    }
  }

  await api('fill', {origin, observed: observed()});
  $('#browser-state').textContent = 'Fields checked against the visible page';
  await load();
};

$('#conflict').onclick = async () => {
  await api('conflict');
  await load();
};

$('#recover').onclick = async () => {
  if (browserMode === 'playwright') {
    $('#browser-state').textContent = 'Playwright inspecting live DOM...';
    try {
      await api('automation/recover');
      await updateScreenshot();
      $('#browser-state').textContent = 'Playwright inspected live page · reconciled';
    } catch (e) {
      $('#browser-state').textContent = 'Playwright recovery error: ' + e.message;
    }
    await load();
    return;
  }

  const origin = currentOrigin();
  await api('recover', {origin, observed: observed()});
  $('#browser-state').textContent = 'Actual page read · saved actions reconciled';
  await load();
};

$('#submit-prepare').onclick = async () => {
  await api('submit/prepare');
  await load();
};

$('#submit-approve').onclick = async () => {
  await api('submit/approve');
  await load();
};

$('#chat-compose').addEventListener('submit', sendChatMessage);
$('#inspector').addEventListener('click', async event => {
  const respond = event.target.closest('[data-workflow-respond]');
  if (respond) {
    const decisionId = respond.dataset.workflowRespond;
    const input = $(`[data-workflow-decision-input="${CSS.escape(decisionId)}"]`);
    if (!input?.value.trim()) { alert('Enter a response before saving.'); return; }
    respond.disabled = true;
    try { await api(`workflows/${respond.dataset.workflowId}/decisions/${decisionId}/respond?session_id=${encodeURIComponent(sessionId)}`, {decision: input.value.trim()}); await load(); }
    finally { respond.disabled = false; }
    return;
  }
  const button = event.target.closest('[data-workflow-action]');
  if (!button) return;
  button.disabled = true;
  try { await api(`workflows/${button.dataset.workflowId}/${button.dataset.workflowAction}?session_id=${encodeURIComponent(sessionId)}`); await load(); }
  finally { button.disabled = false; }
});

document.querySelectorAll('.tab').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    selected = b.dataset.tab;
    load();
    if (selected === 'opportunities') loadSavedOpportunities();
    if (selected === 'resumes') loadResumes();
  };
});

$('#inspector').addEventListener('click', async event => {
  const target = event.target;
  try {
    if (target.matches('[data-save-opportunity]')) await saveSearchOpportunity(target.dataset.saveOpportunity);
    else if (target.matches('[data-prepare-opportunity]')) await beginApplicationPreparation(target.dataset.prepareOpportunity);
    else if (target.matches('[data-resume-prep]')) {
      const response = await fetch(`/api/preparations/${encodeURIComponent(target.dataset.resumePrep)}`);
      if (!response.ok) throw new Error('Could not reopen this preparation.');
      currentPreparation = (await response.json()).preparation;
      externalPreparation = true;
      setMode('playwright');
      selected = 'opportunities';
      $('#inspector').innerHTML = renderPreparation(currentPreparation);
      await updateScreenshot();
    }
    else if (target.matches('[data-prep-back]')) { currentPreparation = null; externalPreparation = false; $('#inspector').innerHTML = renderSavedOpportunities(); }
    else if (target.matches('[data-prep-review]')) {
      const prep = currentPreparation, fieldId = target.dataset.fieldId;
      const result = await api(`preparations/${encodeURIComponent(prep.id)}/fields/${encodeURIComponent(fieldId)}/review`, {decision: target.dataset.prepReview}, 'PUT');
      await updatePreparation(result);
    } else if (target.matches('[data-prep-share]')) {
      openPermissionsModal(target.dataset.prepShare, target.dataset.shareOrigin);
    } else if (target.matches('[data-prep-approve-plan]')) {
      const result = await api(`preparations/${encodeURIComponent(currentPreparation.id)}/approve`, {approved: true});
      await updatePreparation(result);
    } else if (target.matches('[data-prep-fill]')) {
      const result = await api(`preparations/${encodeURIComponent(currentPreparation.id)}/fill`, {});
      await updatePreparation(result);
      await updateScreenshot();
    } else if (target.matches('[data-prep-recover]')) {
      const result = await api(`preparations/${encodeURIComponent(currentPreparation.id)}/recover`, {});
      await updatePreparation(result);
      await updateScreenshot();
    }
    else if (target.matches('[data-select-resume]')) await loadResumes(target.dataset.selectResume);
    else if (target.matches('[data-delete-resume]')) {
      if (!confirm('Remove this resume and its unreviewed extraction data? Facts already added to your Passport will remain.')) return;
      await api('resumes/' + encodeURIComponent(target.dataset.deleteResume), {}, 'DELETE');
      await loadResumes(null);
    } else if (target.matches('[data-accept-resume-item]')) await reviewResumeItems('approve', [target.dataset.acceptResumeItem]);
    else if (target.matches('[data-reject-resume-item]')) await reviewResumeItems('reject', [target.dataset.rejectResumeItem]);
    else if (target.matches('[data-approve-selected]') || target.matches('[data-reject-selected]')) {
      const ids = [...document.querySelectorAll('[data-review-select]:checked')].map(input => input.dataset.reviewSelect);
      await reviewResumeItems(target.matches('[data-approve-selected]') ? 'approve' : 'reject', ids);
    } else if (target.matches('[data-edit-resume-item]')) {
      const itemId = target.dataset.editResumeItem;
      const item = selectedResume?.items.find(candidate => candidate.id === itemId);
      if (!item) return;
      const value = prompt('Review or edit this extracted information', item.value);
      if (value === null) return;
      await api(`resumes/${encodeURIComponent(selectedResumeId)}/items/${encodeURIComponent(itemId)}`, {value}, 'PUT');
      await loadResumes(selectedResumeId);
    }
    else if (target.matches('[data-remove-saved]')) {
      await api('opportunities/saved/' + encodeURIComponent(target.dataset.removeSaved), {}, 'DELETE');
      await loadSavedOpportunities();
    } else if (target.matches('[data-add-note]')) {
      const opportunityId = target.dataset.addNote;
      const input = document.querySelector(`[data-note-input="${CSS.escape(opportunityId)}"]`);
      if (!input?.value.trim()) return;
      await api(`opportunities/saved/${encodeURIComponent(opportunityId)}/notes`, {note: input.value.trim()});
      await loadSavedOpportunities();
    } else if (target.matches('[data-edit-note]')) {
      const opportunityId = target.dataset.opportunityId, noteId = target.dataset.editNote;
      const record = savedOpportunities.find(item => item.id === opportunityId);
      const note = record?.notes.find(item => item.id === noteId);
      if (!note) return;
      const updated = prompt('Edit private note', note.note);
      if (updated === null) return;
      await api(`opportunities/saved/${encodeURIComponent(opportunityId)}/notes/${encodeURIComponent(noteId)}`, {note: updated}, 'PUT');
      await loadSavedOpportunities();
    } else if (target.matches('[data-delete-note]')) {
      await api(`opportunities/saved/${encodeURIComponent(target.dataset.opportunityId)}/notes/${encodeURIComponent(target.dataset.deleteNote)}`, {}, 'DELETE');
      await loadSavedOpportunities();
    }
  } catch { /* api() reports request failures */ }
});

$('#inspector').addEventListener('submit', event => {
  if (event.target.matches('#resume-upload')) {
    event.preventDefault();
    uploadResume(event.target);
  }
});

$('#inspector').addEventListener('change', async event => {
  const target = event.target;
  try {
    if (target.id === 'saved-filter') { savedFilter = target.value; await loadSavedOpportunities(); }
    else if (target.id === 'deadline-sort') { sortDeadlines = target.checked; await loadSavedOpportunities(); }
    else if (target.matches('[data-status-opportunity]')) {
      await api(`opportunities/saved/${encodeURIComponent(target.dataset.statusOpportunity)}/status`, {status: target.value}, 'PUT');
      await loadSavedOpportunities();
    } else if (target.matches('[data-prep-map]') && target.value) {
      const result = await api(`preparations/${encodeURIComponent(currentPreparation.id)}/fields/${encodeURIComponent(target.dataset.prepMap)}/review`, {decision: 'map', fact_id: target.value}, 'PUT');
      await updatePreparation(result);
    }
  } catch { /* api() reports request failures */ }
});

window.addEventListener('message', () => {});
browser.onload = () => {
  try {
    const doc = browser.contentDocument;
    const name = doc?.querySelector('h1')?.textContent;
    if (name) {
      $('#site-name').textContent = name;
      const u = new URL(browser.src, window.location.origin);
      $('#address').textContent = u.host + u.pathname;
    }
  } catch {}
};

navigate('/portal/source', 'Student profile');
load();
