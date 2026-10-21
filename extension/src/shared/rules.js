/* Question classification, answer lookup and option matching.
 *
 * Pure functions with no DOM access, shared by the content scripts, the
 * options page and the Node unit tests. Every rule errs toward leaving a
 * field for the user: a question is only filled when it is recognized with
 * confidence and the user has saved an answer for it.
 */
(function (root) {
  "use strict";

  const PROFILE_FIELDS = [
    ["firstName", "First name"], ["lastName", "Last name"], ["preferredName", "Preferred first name (empty = first name)"],
    ["email", "Email"], ["phone", "Phone"],
    ["linkedin", "LinkedIn URL"], ["github", "GitHub URL"], ["portfolio", "Portfolio / website URL"],
    ["location", "City, State (e.g. Boston, MA)"], ["country", "Country"], ["postalCode", "ZIP / postal code"],
    ["company", "Current or most recent company"], ["currentTitle", "Current or most recent job title"],
    ["school", "School"], ["degree", "Degree (e.g. Bachelor of Science)"], ["major", "Major / field of study"],
    ["gradDate", "Graduation (e.g. May 2026)"],
    ["languages", "Languages you speak (comma separated)"],
  ];

  const YES_NO = ["", "yes", "no"];
  // [key, question shown in options, allowed values or null for free text]
  const STANDARD_ANSWERS = [
    ["workAuth", "Are you legally authorized to work in the United States?", YES_NO],
    ["authUnrestricted", "Are you authorized to work in the US without ever needing sponsorship (now or later)?", YES_NO],
    ["sponsorNow", "Do you need visa sponsorship now?", YES_NO],
    ["sponsorFuture", "Will you need visa sponsorship in the future (e.g. H-1B after OPT)?", YES_NO],
    ["citizen", "Are you a U.S. citizen or permanent resident?", YES_NO],
    ["workAuthText", "Work authorization, when asked in words (e.g. F-1 OPT, eligible for STEM extension)", null],
    ["sponsorshipText", "Sponsorship, when asked in words (e.g. Yes, I will need H-1B sponsorship)", null],
    ["relocate", "Are you willing to relocate?", YES_NO],
    ["over18", "Are you at least 18 years old?", YES_NO],
    ["hearAbout", "How did you hear about this job? (e.g. LinkedIn)", null],
  ];

  const DECLINE = "decline";
  const EEO_ANSWERS = [
    ["eeoGender", "Gender", ["Male", "Female", "Non-binary"]],
    ["eeoTransgender", "Transgender", ["Yes", "No"]],
    ["eeoOrientation", "Sexual orientation", ["Heterosexual", "Gay", "Lesbian", "Bisexual"]],
    ["eeoRace", "Race / ethnicity", ["Asian", "Black or African American", "Hispanic or Latino", "White",
      "American Indian or Alaska Native", "Native Hawaiian or Other Pacific Islander", "Two or More Races"]],
    ["eeoHispanic", "Hispanic or Latino", ["Yes", "No"]],
    ["eeoVeteran", "Veteran status", ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran"]],
    ["eeoDisability", "Disability", ["No, I do not have a disability", "Yes, I have a disability"]],
  ];

  const DEFAULT_ANSWERS = Object.fromEntries([
    ...STANDARD_ANSWERS.map(([k]) => [k, ""]),
    ...EEO_ANSWERS.map(([k]) => [k, DECLINE]),
  ]);
  const PROFILE_DEFAULTS = { country: "United States", languages: "English" };
  const DEFAULT_PROFILE = Object.fromEntries(PROFILE_FIELDS.map(([k]) => [k, PROFILE_DEFAULTS[k] || ""]));
  const DEFAULT_SETTINGS = {
    autoRun: false,            // fill as soon as an application form appears
    remember: true,            // save answers typed into custom questions
    fillRemembered: true,      // reuse saved short answers automatically
    draftBackend: "chatgpt",   // one of DRAFT_BACKENDS
    helper: true,              // offer drafting through the local helper
    aiFill: true,              // answer the remaining questions with the helper after filling
  };
  const DRAFT_BACKENDS = ["chatgpt", "claude-code", "manual"];

  // Saved settings over the defaults. A drafting backend that no longer
  // exists (the local Ollama model was removed) falls back to ChatGPT;
  // `notice` says so, once, until the settings are saved again.
  function withDefaults(saved) {
    const settings = Object.assign({}, DEFAULT_SETTINGS, saved);
    let notice = "";
    if (!DRAFT_BACKENDS.includes(settings.draftBackend)) {
      notice = `Drafting with "${settings.draftBackend}" is no longer available, so drafts now use ChatGPT. Pick another under Drafting answers.`;
      settings.draftBackend = DEFAULT_SETTINGS.draftBackend;
    }
    return { settings, notice };
  }

  const US_STATES = {
    AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut",
    DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois",
    IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland",
    MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana",
    NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York",
    NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania",
    RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah",
    VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
  };

  /* ------------------------------------------------------------------ text */

  function clean(s) {
    return String(s == null ? "" : s).replace(/[*✱]/g, " ").replace(/\(\s*(required|optional)\s*\)/gi, " ")
      .replace(/\s+/g, " ").trim();
  }

  /** Key for remembering an answer: case, punctuation and required markers don't matter. */
  function normQ(s) {
    return clean(s).toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
      .replace(/[^a-z0-9]+/g, " ").trim().slice(0, 300);
  }

  /* -------------------------------------------------------- classification */

  const TEXT_KINDS = new Set(["text", "email", "tel", "url", "number", "search", "combo", "prompt"]);
  const CHOICE_KINDS = new Set(["select", "rselect", "radio", "checkbox", "yesno", "listbutton", "prompt"]);

  // Things the user must answer personally, whatever is saved.
  const NEVER = new RegExp([
    "signature", "\\bsign (here|below)", "e ?sign", "captcha", "password", "social security", "\\bssn\\b",
    "date of birth", "birth ?date", "\\bdob\\b", "driver s licen", "drivers licen",
    "salary", "compensation", "\\bpay (expectation|range|requirement)", "desired pay", "expected pay", "hourly rate",
    "consent", "\\bi (agree|acknowledge|certify|confirm|attest|understand|have read)",
    "\\backnowledge", "terms (and|&) conditions", "privacy (policy|notice)", "background check", "drug (test|screen)",
    "criminal", "convicted", "felony", "\\bpronouns?\\b", "\\bitar\\b", "export control", "\\bu s person\\b",
    "security clearance", "\\bclearance\\b", "demographic", "self identif", "phone (device )?type", "device type",
    "country (phone )?code", "phone code", "\\bextension\\b", "address line", "street address", "^address",
    "\\bmiddle name", "twitter", "facebook", "instagram", "\\bx com\\b",
  ].join("|"));

  const EEO_SECTION = /self identif|demographic|equal (employment )?opportunit|\beeo\b|voluntary (disclosure|self)|diversity/;
  const NOT_ME = /emergency|reference|\breferr|recruiter|hiring manager|supervisor|spouse|parent|guardian|relative/;
  const WRITING = /\bwhy\b|tell us|describe|explain|share (a|an|any|with|more|something)|what (excites|interests|motivates|draws|attracts)|cover letter|additional information|anything else|\bessay\b/;

  function yesNoStyle(t) {
    return /^(do|does|will|would|are|is|have|has|can|could|did|were|was|may)\b/.test(t);
  }

  /**
   * What a question asks for. Returns one of
   *   { cat: "file", key: "resume" }        resume upload
   *   { cat: "profile", key, transform }    a profile detail
   *   { cat: "std", key, mode }             a standard answer (mode "yesno" or "text")
   *   { cat: "eeo", key }                   demographic question
   *   { cat: "never", why }                 the user must answer it
   *   { cat: "custom", writing }            anything else; may be remembered
   * label is the question text, attrs extra hints (id, name, automation id),
   * section the heading of the form section the question sits in.
   */
  function classify(label, kind, attrs, section) {
    const t = normQ(label);
    const a = normQ(attrs || "");
    const both = (t + " " + a).trim();
    const short = t.length <= 60;
    const textual = TEXT_KINDS.has(kind) || kind === "textarea";

    if (kind === "file") {
      // A generic "Upload a file" label takes its meaning from the section it is in.
      const generic = !t || /^(upload|attach|select|choose|drop)\b.{0,20}$/.test(t);
      if (/resume|\bcv\b|curriculum/.test(both + (generic ? " " + normQ(section) : "")) && !/cover/.test(t)) return { cat: "file", key: "resume" };
      return { cat: "never", why: "attach this yourself" };
    }
    if (NEVER.test(t) || (!t && NEVER.test(a))) return { cat: "never", why: "answer this yourself" };

    // Demographic (EEO) questions: only ever filled with the saved choice.
    if (/transgender/.test(t)) return { cat: "eeo", key: "eeoTransgender" };
    if (/sexual orientation|lgbt|queer/.test(t)) return { cat: "eeo", key: "eeoOrientation" };
    if (/\brace\b|ethnic/.test(t)) return { cat: "eeo", key: "eeoRace" };
    if (/hispanic|latin[oax]/.test(t)) return { cat: "eeo", key: "eeoHispanic" };
    if (/\bgender\b|\bsex\b/.test(t)) return { cat: "eeo", key: "eeoGender" };
    if (/veteran|military|armed forces/.test(t)) return { cat: "eeo", key: "eeoVeteran" };
    if (/disabilit/.test(t)) return { cat: "eeo", key: "eeoDisability" };
    // Unrecognized questions in a demographic section are demographic too.
    if (EEO_SECTION.test(normQ(section))) return { cat: "never", why: "answer this yourself" };

    // Work authorization and sponsorship.
    const asWords = textual && !yesNoStyle(t);
    if (/unrestricted|without (the )?(need for |needing |requiring |requirement of )?(any )?(employer |visa |company )?sponsorship|not require sponsorship (now|at any)/.test(t)) {
      return { cat: "std", key: "authUnrestricted", mode: "yesno" };
    }
    if (!yesNoStyle(t) && /(visa|immigration|citizenship|work authori[sz]ation|work permit) (status|type)|current (visa|immigration)|type of (visa|work authori)|which visa|what visa/.test(t)) {
      return { cat: "std", key: "workAuthText", mode: "text" };
    }
    if (/sponsor|\bvisa\b|immigration|h ?1 ?b/.test(t)) {
      if (asWords) return { cat: "std", key: "sponsorshipText", mode: "text" };
      const future = /future|later|at any (point|time)|eventually|going forward|ever\b/.test(t);
      const now = /\bnow\b|current|presently|today|at this time|start/.test(t);
      return { cat: "std", key: now && !future ? "sponsorNow" : future && !now ? "sponsorFuture" : "sponsorAny", mode: "yesno" };
    }
    if (/citizen|permanent resident|green card|lawful permanent/.test(t)) return { cat: "std", key: "citizen", mode: "yesno" };
    if (/authori[sz]ed to work|authori[sz]ation to work|eligible to work|eligibility to work|legally (able|allowed|permitted|eligible|authori[sz]ed) to|right to work|legally work|work legally|employment eligibility|work in the (united states|us|u s)\b.*\?/.test(t)) {
      if (asWords) return { cat: "std", key: "workAuthText", mode: "text" };
      return { cat: "std", key: "workAuth", mode: "yesno" };
    }
    if (/relocat/.test(t)) return { cat: "std", key: "relocate", mode: "yesno" };
    if (/\b18 years|at least 18|over 18|age of 18|18 or older|legal age/.test(t)) return { cat: "std", key: "over18", mode: "yesno" };
    if (/how did you (hear|learn|find|come across)|where did you (hear|learn|find|see)|hear about (us|this|the)|referral source|source of (your )?application|^source$|how you heard/.test(t)) {
      return { cat: "std", key: "hearAbout", mode: "text" };
    }

    // "Share a link to your work" wants a URL, not prose.
    const writing = kind === "textarea" || (WRITING.test(t) && !/\blink\b|\burl\b/.test(t));

    // Education and language questions are often long ("Which university are
    // you currently attending or did you last attend? Please select...").
    if (!writing && !/high school|secondary school/.test(t)) {
      // Only spoken languages: "Which languages do you use most?" on a developer form means code.
      if (/\blanguages?\b/.test(t) && !/programming|coding|computer|software|technical|tech stack|framework/.test(t)
          && (/speak|spoken|fluen|proficien|language skill|native|bilingual|written/.test(t) || /^languages?( s)?$/.test(t))) {
        return { cat: "profile", key: "languages" };
      }
      if (/which (university|college|school)|(university|college|school) (are|were|do|did) you|currently attending|last attend/.test(t)) {
        return { cat: "profile", key: "school" };
      }
      if (/graduation (year|date)|year of graduation|(expected|intended|anticipated) graduation|graduat\w* year/.test(t)) {
        return { cat: "profile", key: "gradDate", transform: /year/.test(t) ? "year" : null };
      }
    }

    // Profile details. Long questions that merely mention "email" or
    // "company" are someone else's business, not a profile field.
    if (!NOT_ME.test(t) && !writing && (short || !t)) {
      const p = profileKey(t || a, kind) || attrProfileKey(a, kind, t);
      if (p) return p;
    }
    return { cat: "custom", writing };
  }

  function profileKey(s, kind) {
    const textual = TEXT_KINDS.has(kind);
    if (/preferred (first )?name|nick ?name/.test(s)) return textual ? { cat: "profile", key: "preferredName" } : null;
    if (textual) {
      if (/\bfirst name\b|given name|\bfname\b|first_name|firstname/.test(s)) return { cat: "profile", key: "firstName" };
      if (/\blast name\b|family name|surname|\blname\b|last_name|lastname/.test(s)) return { cat: "profile", key: "lastName" };
      if (/full (legal )?name|legal name|^name$|^your name$|candidate name|^full name/.test(s)) return { cat: "profile", key: "fullName" };
      if (/e ?mail/.test(s)) return { cat: "profile", key: "email" };
      if (/phone|mobile|telephone|\bcell\b/.test(s)) return { cat: "profile", key: "phone" };
      if (/linked ?in/.test(s)) return { cat: "profile", key: "linkedin" };
      if (/git ?hub/.test(s)) return { cat: "profile", key: "github" };
      if (/portfolio|personal (web ?)?site|website|\bblog\b|other (link|url|website)|^url$|web address/.test(s)) return { cat: "profile", key: "portfolio" };
      if (/zip|postal/.test(s)) return { cat: "profile", key: "postalCode" };
      if (/current (job )?title|current role|most recent (job )?title|^job title$|^title$/.test(s)) return { cat: "profile", key: "currentTitle" };
    }
    // "Location (City)" wants a place search, so whole-location wording wins over "city".
    if (/^(current )?location|where are you (currently )?(located|based)|current address|location city|^city state/.test(s)) return { cat: "profile", key: "location" };
    if (/^(current )?city$|^city\b|\bcity$/.test(s)) return { cat: "profile", key: "location", transform: "city" };
    if (/^state\b|state province|^province|\bstate$/.test(s) && !/statement/.test(s)) return { cat: "profile", key: "location", transform: "state" };
    if (/\bcountry\b/.test(s)) return { cat: "profile", key: "country" };
    if (/current (company|employer)|^company( name)?$|^employer|most recent (company|employer)|^org$|^organi[sz]ation$/.test(s)) return { cat: "profile", key: "company" };
    if (/high school|secondary school/.test(s)) return null;
    if (/school|university|college|institution/.test(s)) return { cat: "profile", key: "school" };
    if (/^degree|degree (type|level|earned|obtained)|highest degree|level of education|education level/.test(s)) return { cat: "profile", key: "degree" };
    if (/discipline|\bmajor\b|field of study|area of study|concentration/.test(s)) return { cat: "profile", key: "major" };
    if (/graduat/.test(s)) return { cat: "profile", key: "gradDate", transform: /year/.test(s) ? "year" : null };
    return null;
  }

  // Lever and Greenhouse put clear names on inputs whose visible label is
  // generic ("Full name" is name="name", LinkedIn is name="urls[LinkedIn]").
  function attrProfileKey(a, kind, t) {
    if (!a || !TEXT_KINDS.has(kind)) return null;
    if (/^(name|systemfield name)$/.test(a)) return { cat: "profile", key: "fullName" };
    if (/\burls linkedin\b/.test(a)) return { cat: "profile", key: "linkedin" };
    if (/\burls github\b/.test(a)) return { cat: "profile", key: "github" };
    if (/\burls portfolio\b|\burls other\b/.test(a)) return { cat: "profile", key: "portfolio" };
    if (/^org$/.test(a)) return { cat: "profile", key: "company" };
    return null;
  }

  /* ---------------------------------------------------------------- answers */

  /** The saved value for a classified question, or "" when there is none. */
  function answerFor(cls, profile, answers) {
    profile = profile || {};
    answers = answers || {};
    if (cls.cat === "profile") {
      if (cls.key === "fullName") return [profile.firstName, profile.lastName].filter(Boolean).join(" ");
      let v = String(profile[cls.key] || "").trim();
      if (cls.key === "preferredName" && !v) v = String(profile.firstName || "").trim();
      if (cls.transform === "city") v = v.split(",")[0].trim();
      if (cls.transform === "state") {
        const st = (v.split(",")[1] || "").trim();
        v = US_STATES[st.toUpperCase()] || st;
      }
      if (cls.transform === "year") v = (v.match(/\b(19|20)\d{2}\b/) || [""])[0];
      return v;
    }
    if (cls.cat === "std") {
      if (cls.key === "sponsorAny") {
        const n = answers.sponsorNow, f = answers.sponsorFuture;
        if (n === "yes" || f === "yes") return "yes";
        if (n === "no" && f === "no") return "no";
        return "";
      }
      return String(answers[cls.key] || "").trim();
    }
    if (cls.cat === "eeo") return String(answers[cls.key] || "").trim();
    return "";
  }

  /* --------------------------------------------------------- option choice */

  const DECLINE_RE = /decline|prefer not|not (wish|want) to|don ?t (wish|want) to|do not (wish|want) to|choose not to|rather not|not to (say|answer|disclose|identify|self identify)|wish not to|no answer|not specified|undisclosed/;

  /**
   * Which of the options matches the saved value: returns its index or -1.
   * Ambiguous matches return -1 so the user decides.
   */
  function pickOption(value, options) {
    const v = normQ(value);
    if (!v) return -1;
    const opts = options.map(normQ);
    const one = (pred) => {
      const hits = [];
      opts.forEach((o, i) => { if (o && pred(o)) hits.push(i); });
      return hits.length === 1 ? hits[0] : hits.length > 1 ? -2 : -1;
    };
    if (v === DECLINE) {
      const i = one((o) => DECLINE_RE.test(o));
      return i >= 0 ? i : -1;
    }
    let i = opts.indexOf(v);
    if (i >= 0) return i;
    if (v === "yes" || v === "no") {
      i = one((o) => o === v || o.startsWith(v + " "));
      if (i >= 0) return i;
      // "I am authorized..." style options carry no yes/no word.
      return -1;
    }
    const word = (o, w) => new RegExp("(^| )" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "( |$)").test(o);
    for (const pred of [(o) => o.startsWith(v + " "), (o) => v.startsWith(o + " ") && o.length > 2, (o) => word(o, v)]) {
      i = one(pred);
      if (i >= 0) return i;
      if (i === -2) return -1;
    }
    return -1;
  }

  /** Text to type when a yes/no answer goes into a text box. */
  function asText(cls, value) {
    if (cls.mode === "yesno" || cls.cat === "eeo") {
      if (value === "yes") return "Yes";
      if (value === "no") return "No";
      if (value === DECLINE) return "";
    }
    return value;
  }

  const COUNTRY_ALIASES = {
    "united states": ["United States of America", "USA", "US", "U.S."],
    "usa": ["United States", "United States of America"],
    "united states of america": ["United States", "USA"],
  };

  /** The saved value plus spellings forms commonly use for the same answer. */
  function alternatives(cls, value) {
    const out = [value];
    // "English, Spanish" is several answers to one check-all-that-apply question.
    if (cls.key === "languages") return String(value).split(/[,;/]/).map((x) => x.trim()).filter(Boolean);
    const v = normQ(value);
    if (cls.key === "country") out.push(...(COUNTRY_ALIASES[v] || []));
    if (cls.key === "degree") {
      if (/^(bachelor|b ?s\b|b ?a\b|b ?e\b|b ?tech|bsc)/.test(v)) out.push("Bachelor's Degree", "Bachelor's", "Bachelors", "Bachelor");
      if (/^(master|m ?s\b|m ?a\b|m ?eng|msc|m ?tech)/.test(v)) out.push("Master's Degree", "Master's", "Masters", "Master");
      if (/^(ph ?d|doctor)/.test(v)) out.push("Doctorate", "Ph.D.", "PhD");
    }
    if (cls.key === "location" && !cls.transform) {
      // "Boston, MA" is listed as "Boston, Massachusetts, United States".
      const [city, st] = String(value).split(",").map((x) => x.trim());
      const full = st && US_STATES[st.toUpperCase()];
      if (city && full) out.push(`${city}, ${full}, United States`, `${city}, ${full}`, `${city}, ${st.toUpperCase()}, USA`);
    }
    if (cls.transform === "state") {
      const abbr = Object.keys(US_STATES).find((k) => US_STATES[k] === value);
      if (abbr) out.push(abbr);
    }
    return [...new Set(out.filter(Boolean))];
  }

  root.JR = root.JR || {};
  root.JR.rules = {
    PROFILE_FIELDS, STANDARD_ANSWERS, EEO_ANSWERS, DECLINE, DEFAULT_ANSWERS, DEFAULT_PROFILE, DEFAULT_SETTINGS, DRAFT_BACKENDS, withDefaults,
    US_STATES, CHOICE_KINDS, TEXT_KINDS, clean, normQ, classify, answerFor, pickOption, asText, alternatives, DECLINE_RE,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
