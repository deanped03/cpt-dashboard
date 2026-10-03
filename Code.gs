/**
 * Auto-Highlight Best Billing-Method Rate — FIXED-GRID REWRITE (2026-07-30)
 * ---------------------------------------------------------------------------
 * WHY THIS WAS REWRITTEN: the old version detected each insurance plan's
 * 4 columns by scanning header rows for the literal word "Alma" and using
 * that as the anchor to find a merged company-name cell above it. That
 * worked under the OLD sheet layout, where every plan's Alma column always
 * had text in it. It silently BREAKS now that a plan can legitimately have
 * only one channel filled in for a given state (e.g. Florida's BCBS
 * Nebraska has only a Headway entry — Alma/Grow/SBH are blank there). With
 * no "Alma" text to anchor on, the old code never even recognized that
 * plan+state combination as a block, so it never got scanned or
 * highlighted at all — this is the exact bug Dean reported (BCBS Nebraska's
 * lone Headway rate in Florida wasn't being marked as the winner, even
 * though by definition it's the only rate and therefore the best one).
 *
 * THE FIX: since 2026-07-29 the MASTER sheet uses a fixed grid — every one
 * of the 33 plans (5 core + 28 Blue Cross) always occupies the exact same
 * 4 columns in every state block, no exceptions. There's no need to detect
 * anything by scanning text anymore; the column positions are just known.
 * This version compares strictly within each plan's own 4 columns, for
 * each state and each CPT row independently, using whichever of the 4
 * channels actually has a rate — including the case where only one
 * channel has data, which now always wins automatically, using its own
 * header tag's color.
 *
 * SHEET CONVENTION (unchanged):
 *   Tag a channel's header cell with a bracket prefix, e.g.:
 *     [COMMON] Alma 03/31/26   (or no bracket at all — same as [COMMON])
 *     [JJ] Headway BCBS Nebraska 07/22/26
 *     [KR] Grow BCBS of MA 6/30/26
 *     [LK] SBH 6/30/26
 *
 * COLOR KEY:
 *   Green  = [COMMON] (or untagged) rate wins for that CPT row
 *   Blue   = JJ (Jodene)'s rate wins
 *   Mauve  = KR (Katherine/Katie)'s rate wins
 *   Orange = LK (Lori)'s rate wins
 *
 * COMPARISON RULE (confirmed with Dean 2026-07-30): every Blue Cross plan
 * is compared only against ITSELF — never against another Blue Cross
 * plan (BCBS Nebraska is never compared to BCBS Texas). Within one plan,
 * for one state, for one CPT code: look at whichever of Alma/Headway/
 * Grow/SBH actually has a rate entered (channels with no header label at
 * all are ignored, not treated as $0), take the highest of those, and
 * highlight it. If only one channel has data, it wins by default.
 *
 * INSTALL: Extensions -> Apps Script on the MASTER "Reimbursement Rates"
 * spreadsheet, replace all existing code with this file's contents, Save,
 * run onOpen once to reauthorize + refresh the menu, reload the sheet.
 *
 * REAL-TIME UPDATES: onEdit(e) still runs automatically on every cell
 * edit and recomputes just the affected state's rows.
 */

// ---- Configuration ----
var METHODS = ['Alma', 'Headway', 'Grow', 'SBH'];
var PROVIDER_TAGS = ['JJ', 'KR', 'LK'];
var TAG_PATTERN = new RegExp('^\\s*\\[(COMMON|' + PROVIDER_TAGS.join('|') + ')\\]', 'i');
var SHEET_NAME = 'Rates'; // scoped to the Master sheet's Rates tab only — do not leave null

var PROVIDER_COLORS = {
  COMMON: '#44966E', // green  - common/shared rate wins
  JJ:     '#6F9AC3', // blue   - JJ's (Jodene's) rate wins
  KR:     '#d5a6bd', // mauve  - KR's (Katherine/Katie's) rate wins
  LK:     '#f9cb9c'  // orange - LK's (Lori's) rate wins
};
var INK = '#2B2716';
var LEGEND_CELL = 'A1';

// ---- Fixed grid — must match populate_slave_sheet_code_v12.gs exactly ----
var CORE_GROUPS = [
  { plan: 'Optum/UHC/Oscar',           col: 2  },
  { plan: 'Aetna',                     col: 6  },
  { plan: 'Cigna',                     col: 10 },
  { plan: 'Carelon Behavioral Health', col: 14 },
  { plan: 'Ambetter',                  col: 18 },
];
var BLUE_CROSS_PLANS = [
  'Anthem BCBS Colorado','Anthem BCBS Connecticut','Anthem BCBS Indiana','Anthem BCBS Maine',
  'Anthem BCBS Nevada','Anthem BCBS New Hampshire','Anthem BCBS Virginia','Anthem Blue Cross California',
  'BCBS Arizona','BCBS CareFirst','BCBS Hawaii','BCBS Massachusetts','BCBS Michigan','BCBS Minnesota',
  'BCBS Minnesota Medicaid','BCBS Montana','BCBS Nebraska','BCBS Texas','Blue Shield of California',
  'Florida Blue','Florida Blue Medicare Advantage','Horizon BCBS New Jersey',
  'Independence Blue Cross Pennsylvania','Premera Blue Cross Washington','Providence Health Plan',
  'Regence BCBS Oregon','Regence BlueShield Washington','Wellmark Iowa',
];
var BLUE_CROSS_START_COL = 22;

function _buildAllPlanGroups() {
  var groups = CORE_GROUPS.slice();
  for (var i = 0; i < BLUE_CROSS_PLANS.length; i++) {
    groups.push({ plan: BLUE_CROSS_PLANS[i], col: BLUE_CROSS_START_COL + i * 4 });
  }
  return groups;
}
var ALL_PLAN_GROUPS = _buildAllPlanGroups();
var FIRST_COL = 2;
var LAST_COL = BLUE_CROSS_START_COL + BLUE_CROSS_PLANS.length * 4 - 1; // 133
var TOTAL_DATA_COLS = LAST_COL - FIRST_COL + 1; // 132

var STATE_TO_ROW = {
  AK:  3,  AZ: 11,  CO: 19,  CT: 27,  DC: 35,  FL: 43,
  HI: 51,  ID: 59,  IA: 67,  KS: 75,  ME: 83,  MD: 91,
  MN: 99,  MT:107,  NE:115,  NV:123,  NH:131,  NM:139,
  ND:147,  OR:155,  SD:163,  UT:171,  VT:179,  WA:187,
  WY:195,
};
var FIRST_BLOCK_ROW = 3;   // AK
var BLOCK_HEIGHT = 8;      // rows per state
var NUM_STATE_BLOCKS = 25;

// ---- Menu ----
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Rate Tools')
    .addItem('Highlight Best Rates', 'highlightBestRates')
    .addItem('Add/Update Legend Note', 'addLegendNote')
    .addItem('Clear Old Conditional Formatting', 'clearConditionalFormatting')
    .addToUi();
}

function addLegendNote() {
  var sheet = getTargetSheet();
  var noteText =
    'RATE HIGHLIGHT LEGEND\n\n' +
    'Green  = Common/shared rate is the best option for this CPT code\n' +
    'Blue   = JJ (Jodene)\'s rate beats the others\n' +
    'Mauve  = KR (Katherine/Katie)\'s rate beats the others\n' +
    'Orange = LK (Lori)\'s rate beats the others\n\n' +
    'Every plan (including every individual Blue Cross plan) is compared ' +
    'only against its own Alma/Headway/Grow/SBH columns for that state — ' +
    'never against a different plan. If only one channel has a rate for a ' +
    'plan in a given state, that channel wins by default.\n\n' +
    'Header tagging: start a channel header cell with [COMMON], [JJ], ' +
    '[KR], or [LK]. Untagged headers are treated as [COMMON].\n\n' +
    'Rate Tools menu -> "Highlight Best Rates" re-scans the whole sheet.';
  sheet.getRange(LEGEND_CELL).setNote(noteText);
  SpreadsheetApp.getActiveSpreadsheet().toast('Legend note added to cell ' + LEGEND_CELL + '.', 'Add Legend Note', 5);
}

function clearConditionalFormatting() {
  var sheet = getTargetSheet();
  var existingRules = sheet.getConditionalFormatRules();
  var ruleCount = existingRules.length;
  var ui = SpreadsheetApp.getUi();
  var response = ui.alert(
    'Clear Conditional Formatting',
    'This will permanently delete all ' + ruleCount + ' conditional formatting rule(s) on "' +
      sheet.getName() + '". Continue?',
    ui.ButtonSet.YES_NO
  );
  if (response !== ui.Button.YES) {
    SpreadsheetApp.getActiveSpreadsheet().toast('Cancelled. No rules were deleted.', 'Clear Conditional Formatting', 5);
    return;
  }
  sheet.setConditionalFormatRules([]);
  SpreadsheetApp.getActiveSpreadsheet().toast(ruleCount + ' conditional formatting rule(s) removed.', 'Clear Conditional Formatting', 5);
}

// ---- Main entry point: full-sheet pass ----
function highlightBestRates() {
  var sheet = getTargetSheet();
  var totalWinners = 0;
  var states = Object.keys(STATE_TO_ROW);

  for (var s = 0; s < states.length; s++) {
    var blockStart = STATE_TO_ROW[states[s]];
    totalWinners += _recomputeStateBlock(sheet, blockStart);
  }

  SpreadsheetApp.getActiveSpreadsheet().toast(
    totalWinners + ' winning cell(s) highlighted across ' + states.length + ' states, ' +
    ALL_PLAN_GROUPS.length + ' plans.',
    'Highlight Best Rates',
    5
  );
}

// ---- Core logic: recompute all 5 CPT rows for one state block, all 33 plans ----
function _recomputeStateBlock(sheet, blockStart) {
  var headerRow = blockStart + 1;
  var firstCptRow = blockStart + 2;

  var headerVals = sheet.getRange(headerRow, FIRST_COL, 1, TOTAL_DATA_COLS).getValues()[0];
  var cptVals = sheet.getRange(firstCptRow, FIRST_COL, 5, TOTAL_DATA_COLS).getValues();

  var bgOut = [];
  var fcOut = [];
  var fwOut = [];
  for (var r = 0; r < 5; r++) {
    bgOut.push(new Array(TOTAL_DATA_COLS).fill('#FFFFFF'));
    fcOut.push(new Array(TOTAL_DATA_COLS).fill(INK));
    fwOut.push(new Array(TOTAL_DATA_COLS).fill('normal'));
  }

  var winners = 0;

  for (var g = 0; g < ALL_PLAN_GROUPS.length; g++) {
    var planCol = ALL_PLAN_GROUPS[g].col;
    var relCol = planCol - FIRST_COL; // 0-indexed offset into this state's read arrays

    var headerTexts = [headerVals[relCol], headerVals[relCol + 1], headerVals[relCol + 2], headerVals[relCol + 3]];
    var tags = headerTexts.map(parseProviderTag);

    for (var cr = 0; cr < 5; cr++) {
      var rawVals = [cptVals[cr][relCol], cptVals[cr][relCol + 1], cptVals[cr][relCol + 2], cptVals[cr][relCol + 3]];
      var candidates = [];
      for (var c = 0; c < 4; c++) {
        var v = parseRate(rawVals[c]);
        var hasHeader = String(headerTexts[c] == null ? '' : headerTexts[c]).trim() !== '';
        if (v !== null && hasHeader) {
          candidates.push({ idx: c, value: v, tag: tags[c] });
        }
      }
      if (candidates.length === 0) continue;

      var best = Math.max.apply(null, candidates.map(function (x) { return x.value; }));
      candidates.forEach(function (cd) {
        if (cd.value === best) {
          var color = PROVIDER_COLORS[cd.tag] || PROVIDER_COLORS.COMMON;
          bgOut[cr][relCol + cd.idx] = color;
          fcOut[cr][relCol + cd.idx] = '#FFFFFF';
          fwOut[cr][relCol + cd.idx] = 'bold';
          winners++;
        }
      });
    }
  }

  var bodyRange = sheet.getRange(firstCptRow, FIRST_COL, 5, TOTAL_DATA_COLS);
  bodyRange.setBackgrounds(bgOut);
  bodyRange.setFontColors(fcOut);
  bodyRange.setFontWeights(fwOut);

  return winners;
}

// ---- Real-time updates ----
function onEdit(e) {
  try {
    var sheet = e.range.getSheet();
    if (SHEET_NAME && sheet.getName() !== SHEET_NAME) return;

    var editedRow = e.range.getRow();
    var lastEditedRow = e.range.getLastRow();

    var blocksToRecompute = {};
    for (var row = editedRow; row <= lastEditedRow; row++) {
      var blockStart = _findStateBlockStart(row);
      if (blockStart !== null) blocksToRecompute[blockStart] = true;
    }

    for (var bs in blocksToRecompute) {
      _recomputeStateBlock(sheet, Number(bs));
    }
  } catch (err) {
    console.error('onEdit highlight error: ' + err);
  }
}

// Given a row number, returns that state block's start row if the row
// falls within a state's header or CPT rows (rows blockStart+1 through
// blockStart+6), or null if it's a label row, spacer row, or outside the
// grid entirely (e.g. Row 1 or Row 3).
function _findStateBlockStart(row) {
  if (row < FIRST_BLOCK_ROW) return null;
  var offset = row - FIRST_BLOCK_ROW;
  var blockIndex = Math.floor(offset / BLOCK_HEIGHT);
  if (blockIndex < 0 || blockIndex >= NUM_STATE_BLOCKS) return null;
  var blockStart = FIRST_BLOCK_ROW + blockIndex * BLOCK_HEIGHT;
  var withinBlock = row - blockStart;
  if (withinBlock < 1 || withinBlock > 6) return null; // 1=header row, 2-6=CPT rows
  return blockStart;
}

// ---- Helpers ----
function getTargetSheet() {
  return SHEET_NAME
    ? SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME)
    : SpreadsheetApp.getActiveSheet();
}

function parseRate(cellValue) {
  if (typeof cellValue === 'number') return cellValue;
  if (typeof cellValue === 'string') {
    var trimmed = cellValue.trim();
    if (/^\$?-?\d[\d,]*(\.\d+)?$/.test(trimmed)) {
      return parseFloat(trimmed.replace(/[$,]/g, ''));
    }
  }
  return null;
}

function parseProviderTag(text) {
  var match = String(text == null ? '' : text).match(TAG_PATTERN);
  return match ? match[1].toUpperCase() : 'COMMON';
}

// ---- Debug helpers ----
function debugProviderTags() {
  var sheet = getTargetSheet();
  var values = sheet.getDataRange().getValues();
  var found = [];
  for (var r = 0; r < values.length; r++) {
    for (var c = 0; c < values[r].length; c++) {
      var text = String(values[r][c] || '').trim();
      if (text === '') continue;
      var match = text.match(TAG_PATTERN);
      if (match) {
        found.push('Row ' + (r + 1) + ' Col ' + (c + 1) + ': tag=[' + match[1] + '] text=' + text.substring(0, 40));
      }
    }
  }
  Logger.log(found.join('\n'));
  SpreadsheetApp.getUi().alert(found.length + ' tagged cells found:\n\n' + found.slice(0, 20).join('\n'));
}

function debugPlanGrid() {
  var lines = [];
  for (var g = 0; g < ALL_PLAN_GROUPS.length; g++) {
    lines.push(ALL_PLAN_GROUPS[g].plan + ': col ' + ALL_PLAN_GROUPS[g].col + '-' + (ALL_PLAN_GROUPS[g].col + 3));
  }
  Logger.log(lines.join('\n'));
}
