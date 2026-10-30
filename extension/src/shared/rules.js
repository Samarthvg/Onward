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
    ["email", "Email"], ["phone", "Phone"], ["phoneType", "Phone type (Mobile, Home or Work)"],
    ["linkedin", "LinkedIn URL"], ["github", "GitHub URL"], ["portfolio", "Portfolio / website URL"],
    ["address", "Street address (e.g. 123 Main St)"],
    ["city", "City (e.g. Boston)"], ["state", "State (e.g. MA)"], ["country", "Country"], ["postalCode", "ZIP / postal code"],
    ["company", "Current or most recent company"], ["currentTitle", "Current or most recent job title"],
    ["school", "School"], ["degree", "Degree (e.g. Bachelor of Science)"], ["major", "Major / field of study"],
    ["gradDate", "Graduation (e.g. May 2026)"],
    ["languages", "Languages you speak, with a level if you like (e.g. English (Fluent), Hindi (Native))"],
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
  const PROFILE_DEFAULTS = { country: "United States", languages: "English", phoneType: "Mobile" };
  const DEFAULT_PROFILE = Object.fromEntries(PROFILE_FIELDS.map(([k]) => [k, PROFILE_DEFAULTS[k] || ""]));
  const DEFAULT_SETTINGS = {
    autoRun: false,            // fill as soon as an application form appears
    fillSteps: true,           // once filled, fill each next step of the form (Workday) as it appears
    remember: true,            // save answers typed into custom questions
    fillRemembered: true,      // reuse saved short answers automatically
    helper: true,              // offer drafting through the Onward app
    aiFill: true,              // answer the remaining questions with the helper after filling
    fitSkills: true,           // add only the resume skills that fit the posting
  };
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
    "security clearance", "\\bclearance\\b", "demographic", "self identif",
    "\\bextension\\b", "address line (2|two)", "^address 2$", "\\bapt\\b", "apartment", "\\bsuite\\b", "\\bcounty\\b",
    "\\bmiddle name", "twitter", "facebook", "instagram", "\\bx com\\b",
  ].join("|"));

  const EEO_SECTION = /self identif|demographic|equal (employment )?opportunit|\beeo\b|voluntary (disclosure|self)|diversity/;
  const NOT_ME = /emergency|reference|\breferr|recruiter|hiring manager|supervisor|spouse|parent|guardian|relative/;
  const WRITING = /\bwhy\b|tell us|describe|explain|share (a|an|any|with|more|something)|what (excites|interests|motivates|draws|attracts)|cover letter|additional information|anything else|\bessay\b/;

  const CONSENT = /consent|\bagree|acknowledg|certif|attest|signature|authori[sz]e (onward|you|the company|us)/;

  /** The demographic question text names, or null. With fromOptions, read from the answer options instead. */
  function eeoKey(t, fromOptions) {
    if (/transgender|cisgender/.test(t)) return "eeoTransgender";
    if (/sexual orientation|lgbt|queer|heterosexual|bisexual/.test(t)) return "eeoOrientation";
    if (/disabilit/.test(t)) return "eeoDisability";
    if (/veteran|military|armed forces/.test(t)) return "eeoVeteran";
    if (fromOptions) {
      if (/\b(asian|white|black or african|two or more races|native hawaiian|american indian)\b/.test(t)) return "eeoRace";
      if (/\b(male|female|non binary|man|woman)\b/.test(t)) return "eeoGender";
      return null;
    }
    if (/\brace\b|ethnic/.test(t)) return "eeoRace";
    if (/hispanic|latin[oax]/.test(t)) return "eeoHispanic";
    if (/\bgender\b|\bsex\b/.test(t)) return "eeoGender";
    return null;
  }

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
  function classify(label, kind, attrs, section, options) {
    const t = normQ(label);
    const a = normQ(attrs || "");
    const both = (t + " " + a).trim();
    const short = t.length <= 60;
    const textual = TEXT_KINDS.has(kind) || kind === "textarea";
    const eeoSection = EEO_SECTION.test(normQ(section));

    if (kind === "file") {
      // A generic "Upload a file" label takes its meaning from the section it is in.
      const generic = !t || /^(upload|attach|select|choose|drop)\b.{0,20}$/.test(t);
      if (/resume|\bcv\b|curriculum/.test(both + (generic ? " " + normQ(section) : "")) && !/cover/.test(t)) return { cat: "file", key: "resume" };
      return { cat: "never", why: "attach this yourself" };
    }
    // The date on a self-identification form ("Date", Workday's dateSignedOn): today.
    if (/date ?signed|signed ?on|todays? ?date/.test(a) || (/^(todays? )?date( signed)?$/.test(t) && eeoSection)) {
      const part = /month/.test(t + " " + a) ? "month" : /\bday\b/.test(t + " " + a) ? "day" : /year/.test(t + " " + a) ? "year" : "date";
      return { cat: "profile", key: "today", transform: part };
    }

    // Demographic (EEO) questions: only ever filled with the saved choice. A
    // consent or attestation that mentions them stays the user's to answer.
    const eeo = !CONSENT.test(t) && eeoKey(t);
    if (eeo) return { cat: "eeo", key: eeo };
    if (NEVER.test(t) || (!t && NEVER.test(a))) return { cat: "never", why: "answer this yourself" };
    // A generic label ("Please check one of the boxes below") is named by its options.
    const byOptions = options && options.length && !CONSENT.test(t) ? eeoKey(normQ(options.join(" ")), true) : null;
    if (byOptions) return { cat: "eeo", key: byOptions };
    // Other choices in a demographic section are demographic too; its Name
    // and Date fields are ordinary details.
    // (The disability form's Language picks the form's language, not a demographic.)
    if (eeoSection && !textual && !/^(form )?languages?$/.test(t)) return { cat: "never", why: "answer this yourself" };

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
    // Before "phone" and "country": Workday's Phone Device Type and Country Phone Code.
    if (/phone (device )?type|device type|type of phone/.test(s)) return { cat: "profile", key: "phoneType" };
    if (/(phone|dialing|calling) (country )?code|country code/.test(s)) return { cat: "profile", key: "country" };
    // A street line, or the whole address when the form has one box for it.
    if (/address line (1|one)$|^address line$|^address 1$|street address|^street$/.test(s)) return textual ? { cat: "profile", key: "address" } : null;
    if (/^(home |mailing |residential |permanent |full )?address$/.test(s)) return textual ? { cat: "profile", key: "address", transform: "full" } : null;
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

  /** Where the applicant lives: { city, state (full name for a US state), abbr }.
   *  Reads the City and State fields, or the single "City, State" one
   *  profiles had before 0.3.1. A ZIP typed after the state is ignored. */
  function place(profile) {
    profile = profile || {};
    let city = String(profile.city || "").trim(), st = String(profile.state || "").trim();
    if (!city && !st && profile.location) [city = "", st = ""] = String(profile.location).split(",").map((x) => x.trim());
    st = st.replace(/\s+\d{5}(-\d{4})?$/, "");
    const abbr = Object.keys(US_STATES).find((k) => k === st.toUpperCase() || US_STATES[k].toLowerCase() === st.toLowerCase()) || "";
    return { city, state: abbr ? US_STATES[abbr] : st, abbr };
  }

  /** The saved value for a classified question, or "" when there is none. */
  function answerFor(cls, profile, answers) {
    profile = profile || {};
    answers = answers || {};
    if (cls.cat === "profile") {
      if (cls.key === "fullName") return [profile.firstName, profile.lastName].filter(Boolean).join(" ");
      if (cls.key === "today") {
        const d = new Date();
        const mm = String(d.getMonth() + 1).padStart(2, "0"), dd = String(d.getDate()).padStart(2, "0");
        return { month: mm, day: dd, year: String(d.getFullYear()) }[cls.transform] || `${mm}/${dd}/${d.getFullYear()}`;
      }
      if (cls.key === "location") {
        const p = place(profile);
        if (cls.transform === "city") return p.city;
        if (cls.transform === "state") return p.state;
        return [p.city, p.abbr || p.state].filter(Boolean).join(", ");
      }
      let v = String(profile[cls.key] || "").trim();
      if (cls.key === "preferredName" && !v) v = String(profile.firstName || "").trim();
      if (cls.transform === "year") v = (v.match(/\b(19|20)\d{2}\b/) || [""])[0];
      // One box for the whole address: "123 Main St, Boston, MA 02115".
      if (cls.transform === "full" && v) {
        const p = place(profile);
        v = [v, p.city, p.abbr || p.state].filter(Boolean).join(", ");
        if (profile.postalCode) v += " " + String(profile.postalCode).trim();
      }
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

  /* --------------------------------------------- saved answers by company */

  const COMPANY_SUFFIX = /\b(inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa|pbc|technologies|technology|labs?|group|holdings|financial|systems|software|solutions|ai|io|hq)\b/g;
  const GENERIC_WORDS = new Set(["the", "and", "for", "with", "global", "international", "national", "american", "united",
    "first", "general", "digital", "data", "cloud", "health", "bank", "capital", "energy", "media", "network", "new", "open"]);

  /** Comparable form of a company name: "ElevenLabs, Inc." and "Elevenlabs" match. */
  function companyKey(name) {
    return normQ(name).replace(COMPANY_SUFFIX, " ").replace(/\s+/g, "");
  }

  function sameCompany(a, b) {
    const x = companyKey(a), y = companyKey(b);
    return !!x && !!y && (x === y || (Math.min(x.length, y.length) >= 4 && (x.startsWith(y) || y.startsWith(x))));
  }

  /** Spellings of a company an answer might use: "Chime Financial, Inc" -> Chime Financial, Chime. */
  function companyNames(name) {
    const full = clean(name).replace(/,?\s*\b(inc|llc|ltd|corp|plc|gmbh|pbc)\b\.?$/i, "").trim();
    if (!full) return [];
    const out = [full];
    const core = full.replace(new RegExp(COMPANY_SUFFIX.source, "gi"), " ").replace(/\s+/g, " ").trim();
    if (core && core !== full) out.push(core);
    const first = core.split(/[\s,]+/)[0];
    if (first && first.length >= 4 && !GENERIC_WORDS.has(first.toLowerCase())) out.push(first);
    return [...new Set(out)];
  }

  /**
   * The company and product names an answer written for one company
   * mentions: the company's own names, and capitalized words of the posting
   * (or job title) the answer repeats that start like the company does
   * ("ElevenMusic" at ElevenLabs) or appear in the job title.
   */
  function namesIn(answer, company, title, posting) {
    const text = String(answer || "");
    const found = mentions(text, companyNames(company));
    const stem = companyKey(company).slice(0, 5);
    const titleWords = new Set((String(title || "").match(/\b[A-Z][A-Za-z0-9]{3,}\b/g) || []).map((w) => w.toLowerCase()));
    const source = String(posting || "") + " " + String(title || "");
    for (const w of new Set(text.match(/\b[A-Z][A-Za-z0-9]*[A-Za-z0-9]{2,}\b/g) || [])) {
      const lw = w.toLowerCase();
      if (found.some((f) => f.toLowerCase() === lw)) continue;
      const product = (stem.length >= 4 && lw.startsWith(stem) && lw !== stem) || (titleWords.has(lw) && /[A-Z].*[A-Z]|\d/.test(w.slice(1)));
      if (product && new RegExp("\\b" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b").test(source + " " + company)) found.push(w);
    }
    return found;
  }

  /** Which of names appear in text as whole words, case-insensitively. */
  function mentions(text, names) {
    const t = " " + normQ(text) + " ";
    return (names || []).filter((n) => { const k = normQ(n); return k && t.includes(" " + k + " "); });
  }

  /**
   * How a saved answer may be used on this page:
   *   "use"   same company, or the answer names no company or product
   *   "warn"  written for another company and names it or its products;
   *           offered only behind a warning, never filled on its own
   */
  function savedAnswerUse(saved, pageCompany) {
    if (!saved || !saved.a) return { use: "none" };
    if (saved.company && pageCompany && sameCompany(saved.company, pageCompany)) return { use: "use", names: [] };
    // Answers saved before names were recorded: catch "ElevenMusic" by the company's stem.
    const stem = companyKey(saved.company).slice(0, 5);
    const stemmed = stem.length >= 5 ? (String(saved.a).match(/\b[A-Z][A-Za-z0-9]{4,}\b/g) || []).filter((w) => w.toLowerCase().startsWith(stem)) : [];
    const names = [...new Set([...(saved.names || []), ...mentions(saved.a, companyNames(saved.company)), ...stemmed])];
    return names.length ? { use: "warn", names, company: saved.company || "another company" } : { use: "use", names: [] };
  }

  /* --------------------------------------- entries relevant to one job */

  const STOP = new Set(("the and for with you your our are was were will have has had this that from into over under about across "
    + "their they them who what when where why how all any each more most other some such than then there these those very can "
    + "able using use used work working worked team teams role job jobs company companies experience years year new build built "
    + "building help helped make made also well strong including include within based across like plus per day days time looking "
    + "join want need needs must should would could get got one two three part full every high low end best good great").split(" "));

  function terms(text) {
    const out = new Set();
    for (const w of String(text || "").toLowerCase().match(/[a-z][a-z0-9+#.]*[a-z0-9+#]|[a-z]/g) || []) {
      const t = w.replace(/\.+$/, "");
      if (t.length >= 3 && !STOP.has(t)) out.add(t);
    }
    return out;
  }

  /**
   * The applicant's saved jobs and projects that relate to this job, as text
   * for the AI: only entries sharing at least `min` distinct terms with the
   * posting and title, at most `max` of each kind, best match first.
   * Everything else stays on this machine.
   */
  function relevantEntries(history, posting, title, { min = 3, max = 3 } = {}) {
    if (!history) return "";
    const want = terms(String(posting || "") + " " + String(title || ""));
    if (!want.size) return "";
    const lines = (e) => (e.description != null ? String(e.description).split("\n") : e.bullets || []).map((l) => l.trim()).filter(Boolean);
    const score = (e, head) => { let n = 0; for (const t of terms(head + " " + (e.tech || "") + " " + lines(e).join(" "))) if (want.has(t)) n++; return n; };
    const date = (d) => (d && d.year ? (d.month ? String(d.month).padStart(2, "0") + "/" : "") + d.year : "");
    const span = (e) => [date(e.start), e.current ? "present" : date(e.end)].filter(Boolean).join(" to ");
    const pick = (list, head) => (Array.isArray(list) ? list : [])
      .map((e) => ({ e, s: score(e, head(e)) })).filter((x) => x.s >= min)
      .sort((a, b) => b.s - a.s).slice(0, max).map((x) => x.e);
    const out = [];
    for (const e of pick(history.work, (e) => [e.title, e.company].join(" "))) {
      out.push(`JOB: ${[e.title, e.company, e.location, span(e)].filter(Boolean).join(", ")}` + lines(e).map((l) => "\n- " + l).join(""));
    }
    for (const e of pick(history.projects, (e) => [e.name, e.role].join(" "))) {
      out.push(`PROJECT: ${[e.name, e.role, e.tech, span(e), e.link].filter(Boolean).join(", ")}` + lines(e).map((l) => "\n- " + l).join(""));
    }
    return out.join("\n\n").slice(0, 6000);
  }

  /* ----------------------------------------------- work history fields */

  // Field names inside repeating Work Experience / Education rows, read from
  // ids and automation ids first ("workExperience-2--jobTitle",
  // "dateSectionMonth-input" under formField-startDate, Greenhouse
  // "company-name--0"), then from specific label wording.
  const HISTORY_ATTR = [
    ["description", /role ?description|job ?description|responsibilit|duties/],
    ["current", /currently ?work|current(ly)?[ _-]?(role|job|employ|attend)|i ?currently|work ?here/],
    ["startMonth", /(start|from|begin)[\w-]*month|month[\w-]*(start|from)|startdate[\w-]*datesectionmonth|startdate.*\bmonth\b/],
    ["startYear", /(start|from|begin|first)[\w-]*year|year[\w-]*(start|from)|startdate[\w-]*datesectionyear|firstyearattended|startdate.*\byear\b/],
    ["endMonth", /(end|to|until)[\w-]*month|month[\w-]*(end|to)\b|enddate[\w-]*datesectionmonth|enddate.*\bmonth\b/],
    ["endYear", /(end|to|until|last|grad)[\w-]*year|year[\w-]*(end|to)\b|enddate[\w-]*datesectionyear|lastyearattended|enddate.*\byear\b/],
    ["startDate", /start[\w-]*date|from[\w-]*date|startdate/],
    ["endDate", /end[\w-]*date|to[\w-]*date|enddate/],
    ["title", /job[\w-]*title|position[\w-]*title|(^|[\s_-])title([\s_-]|$)/],
    ["company", /company|employer|organi[sz]ation/],
    ["location", /location|(^|[\s_-])city([\s_-]|$)/],
    ["school", /school|university|institution|college/],
    ["degree", /degree/],
    ["field", /field[\w-]*of[\w-]*study|fieldofstudy|discipline|major/],
    ["gpa", /\bgpa\b|grade[\w-]*average|gradeaverage|overall ?result/],
  ];
  const HISTORY_LABEL = [
    ["description", /^(role|job|position) description|^(your )?(main )?responsibilities|^description( of (your )?(role|duties))?$/],
    ["current", /^i currently work here|^currently (work|employed|attending) here|^current (role|position|job)$/],
    ["startDate", /^(start|from) date|^date started|^dates? of employment/],
    ["endDate", /^(end|to) date|^date (ended|left)/],
    ["title", /^(job|position) title$|^title$/],
    ["company", /^(company|employer)( name)?$|^name of (your )?(current |previous |last )?(company|employer)/],
    ["school", /^(school|university|college|institution)( name)?$/],
    ["degree", /^degree( (type|name|level|earned))?$/],
    ["field", /^(field of study|major|discipline|area of study)$/],
    ["gpa", /\bgpa\b|grade point average|^overall result/],
  ];

  /** The work or education field a control holds, or null. */
  function historyField(label, attrs) {
    const a = String(attrs || "").toLowerCase();
    if (a) for (const [f, re] of HISTORY_ATTR) if (re.test(a)) return f;
    const t = normQ(label);
    for (const [f, re] of HISTORY_LABEL) if (re.test(t)) return f;
    return null;
  }

  /* --------------------------------------------------------- option choice */

  // Workday also says "Not Declared", "Undeclared" and "I don't wish to self-identify".
  const DECLINE_RE = /decline|prefer not|not (wish|want) to|don ?t (wish|want) to|do not (wish|want) to|choose not to|rather not|not to (say|answer|disclose|identify|self identify)|wish not to|no answer|not specified|undisclosed|not declared|undeclared|not disclosed/;

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

  /** "English (Fluent), Hindi" -> [{ name: "English", level: "Fluent" }, { name: "Hindi", level: "" }]. */
  function languageList(value) {
    return String(value || "").split(/[,;/]/).map((x) => x.trim()).filter(Boolean).map((x) => {
      // "English (Fluent)", "English - Fluent", "English: Fluent"; "Serbo-Croatian" stays whole.
      const m = x.match(/^(.*?)\s*\(\s*([^)]*?)\s*\)$/) || x.match(/^(.*?)\s+[-–]\s+(.+)$/) || x.match(/^(.*?)\s*:\s*(.+)$/);
      return m && m[1] ? { name: m[1].trim(), level: m[2].trim() } : { name: x, level: "" };
    });
  }

  /** The saved value plus spellings forms commonly use for the same answer. */
  function alternatives(cls, value) {
    const out = [value];
    // "English, Spanish" is several answers to one check-all-that-apply question.
    if (cls.key === "languages") return languageList(value).map((l) => l.name);
    const v = normQ(value);
    if (cls.key === "country") out.push(...(COUNTRY_ALIASES[v] || []));
    if (cls.key === "degree") {
      // Workday sites list degrees by abbreviation too: "Bachelor's", "MS", "M.S.", "MBA".
      const sci = /scien|^[bm] ?sc?\b/.test(v), arts = /\barts?\b|^[bm] ?a\b/.test(v);
      const eng = /engineering|^[bm] ?e\b|^[bm] ?eng\b|^[bm] ?tech\b/.test(v);
      if (/^(bachelor|b ?s\b|b ?a\b|b ?e\b|b ?tech|bsc|b ?eng)/.test(v)) {
        out.push("Bachelor's Degree", "Bachelor's", "Bachelors", "Bachelor");
        if (sci) out.push("Bachelor of Science", "BS", "B.S.", "BSc", "B.Sc.");
        if (arts) out.push("Bachelor of Arts", "BA", "B.A.");
        if (eng) out.push("Bachelor of Engineering", "BE", "B.E.", "BEng", "B.Tech", "BTech");
      }
      if (/^(mba\b|master of business)/.test(v)) out.push("MBA", "Master of Business Administration");
      else if (/^(master|m ?s\b|m ?a\b|m ?eng|msc|m ?tech)/.test(v)) {
        out.push("Master's Degree", "Master's", "Masters", "Master");
        if (sci) out.push("Master of Science", "MS", "M.S.", "MSc", "M.Sc.");
        if (arts) out.push("Master of Arts", "MA", "M.A.");
        if (eng) out.push("Master of Engineering", "MEng", "M.Eng.", "M.Tech", "MTech");
      }
      if (/^(ph ?d|doctor)/.test(v)) out.push("Doctorate", "Ph.D.", "PhD", "Doctor of Philosophy");
    }
    if (cls.key === "phoneType") {
      if (/mobile|cell/.test(v)) out.push("Mobile", "Cell", "Mobile Phone", "Cell Phone", "Personal Cell");
      else if (/home|landline|telephone/.test(v)) out.push("Home", "Landline", "Telephone", "Home Phone");
      else if (/work|office|business/.test(v)) out.push("Work", "Business", "Office", "Work Phone");
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

  /* ------------------------------------------------------------------ skills */

  // Sites name skills their own way: "Java (Programming Language)", "C++ Programming
  // Language", "Amazon Web Services (AWS)", "RAG Pipeline". Names in one group are one skill.
  const SKILL_GROUPS = [
    ["AWS", "Amazon Web Services"], ["GCP", "Google Cloud Platform", "Google Cloud"], ["Azure", "Microsoft Azure"],
    ["JavaScript", "JS"], ["TypeScript", "TS"], ["Kubernetes", "K8s"], ["Machine Learning", "ML"], ["Deep Learning", "DL"],
    ["Natural Language Processing", "NLP"], ["Computer Vision", "CV"], ["Artificial Intelligence", "AI"],
    ["Generative AI", "GenAI", "Gen AI"], ["Large Language Models", "LLMs", "LLM"],
    ["Object-Oriented Programming", "OOP"], ["Object-Oriented Design", "OOD"],
    ["Data Structures & Algorithms", "Data Structures and Algorithms", "DSA"],
    ["CI/CD", "CICD", "Continuous Integration and Continuous Delivery", "Continuous Integration/Continuous Deployment", "Continuous Integration"],
    ["REST APIs", "RESTful APIs", "REST", "RESTful", "RESTful Web Services"], ["PostgreSQL", "Postgres"], ["MongoDB", "Mongo"],
    ["TensorFlow", "TF"], ["Scikit-learn", "sklearn"], ["Hugging Face", "HuggingFace"],
    ["RAG", "Retrieval-Augmented Generation", "RAG pipelines"], ["HTML", "HTML5"], ["CSS", "CSS3"],
    ["Node.js", "Node", "NodeJS"], ["React", "React.js", "ReactJS"], ["Vue.js", "Vue", "VueJS"], ["Next.js", "NextJS"],
    ["Express.js", "Express", "ExpressJS"], ["Angular", "AngularJS"], ["Go", "Golang"], ["C#", "C Sharp"], [".NET", "dotnet"],
    ["Microservices", "Microservices Architecture"], ["Unit Testing", "Unit Tests"], ["SQL", "Structured Query Language"],
    ["Git", "Git Version Control"], ["Docker", "Docker Containers"], ["LangChain"], ["LangGraph"],
  ];

  /** A skill name reduced to what identifies it. */
  function skillKey(s) {
    let t = String(s || "").toLowerCase()
      .replace(/c\+\+/g, " cpp ").replace(/c#/g, " csharp ").replace(/\.net\b/g, " dotnet ").replace(/\.js\b/g, "js")
      .replace(/&/g, " and ");
    // What the site adds to say what kind of thing it is.
    t = t.replace(/\(([^)]*)\)/g, (m, inner) => (/language|software|platform|framework|library|tool|database|service|system|technology/.test(inner) ? " " : m))
      .replace(/\b(programming|scripting|markup|query) language\b/g, " ").replace(/\b(framework|library|software)\s*$/g, " ");
    t = t.replace(/[^a-z0-9]+/g, " ").trim();
    t = t.split(" ").filter((w) => w && w !== "and")
      .map((w) => (w.length > 3 && w.endsWith("s") && !w.endsWith("ss") ? w.slice(0, -1) : w)).join(" ");
    return t.replace(/\b([a-z0-9]+) js\b/g, "$1js");
  }

  const SKILL_GROUP_OF = new Map();
  SKILL_GROUPS.forEach((g, i) => g.forEach((n) => SKILL_GROUP_OF.set(skillKey(n), i)));

  /** Every key a skill name stands for: itself, an abbreviation it gives in brackets, and its group. */
  function skillForms(s) {
    const out = new Set([skillKey(s)]);
    const inBrackets = String(s || "").match(/\(([^)]+)\)/);
    if (inBrackets) { out.add(skillKey(inBrackets[1])); out.add(skillKey(String(s).replace(/\([^)]*\)/g, " "))); }
    for (const k of [...out]) {
      if (SKILL_GROUP_OF.has(k)) SKILL_GROUPS[SKILL_GROUP_OF.get(k)].forEach((n) => out.add(skillKey(n)));
      if (/js$/.test(k) && k.length > 4) out.add(k.slice(0, -2));
    }
    out.delete("");
    return out;
  }

  function sameSkill(a, b) {
    const fa = skillForms(a);
    for (const k of skillForms(b)) if (fa.has(k)) return true;
    return false;
  }

  /** "HTML5/CSS3" is two skills; "CI/CD" and "TCP/IP" are one. */
  function splitSkills(list) {
    const out = [];
    for (const s of list || []) {
      const parts = String(s).split(/\s*\/\s*/);
      if (parts.length > 1 && !SKILL_GROUP_OF.has(skillKey(s)) && parts.every((p) => p.length >= 2 && SKILL_GROUP_OF.has(skillKey(p)))) out.push(...parts);
      else out.push(s);
    }
    return [...new Set(out)];
  }

  /** What to type into a site's skill search: the name, then its singular or other names. At most three. */
  function skillSearchTerms(s) {
    const name = String(s || "").trim();
    const terms = [name];
    const singular = name.replace(/\b([A-Za-z]{4,})s\b/g, (m, w) => (/ss$/i.test(m) ? m : w));
    terms.push(singular);
    const g = SKILL_GROUP_OF.get(skillKey(name));
    if (g != null) terms.push(...SKILL_GROUPS[g]);
    const words = name.split(/\s+/);
    if (words.length > 1 && words[0].length >= 2) terms.push(words[0]);
    const seen = new Set();
    return terms.filter((t) => t && !seen.has(t.toLowerCase()) && seen.add(t.toLowerCase())).slice(0, 3);
  }

  // What kind of work a skill belongs to, and the posting words that say a job is that kind.
  // A skill can sit in several areas (Python is backend, data and ML).
  const SKILL_AREAS = {
    frontend: {
      skills: ["HTML", "CSS", "JavaScript", "TypeScript", "React", "Vue.js", "Angular", "Next.js", "Svelte", "Redux", "jQuery",
        "Tailwind CSS", "Sass", "Bootstrap", "Webpack", "Vite", "Figma", "Responsive Design", "Web Accessibility", "DOM", "Jest"],
      words: ["front end", "frontend", "web", "user interface", "ui", "ux", "responsive", "browser", "single page", "full stack", "fullstack"],
    },
    backend: {
      skills: ["Node.js", "Express.js", "REST APIs", "GraphQL", "gRPC", "Django", "Flask", "FastAPI", "Spring Boot", "Spring", "Java",
        "Python", "Go", "C#", ".NET", "Ruby", "Ruby on Rails", "PHP", "Kotlin", "Scala", "Microservices", "SQL", "PostgreSQL", "MySQL",
        "MongoDB", "Redis", "Kafka", "RabbitMQ", "DynamoDB", "Elasticsearch", "Hibernate", "Distributed Systems", "TypeScript"],
      words: ["backend", "back end", "server side", "api", "microservice", "distributed system", "database", "full stack", "fullstack", "scalable"],
    },
    data: {
      skills: ["SQL", "Python", "R", "Pandas", "NumPy", "Apache Spark", "Spark", "PySpark", "Hadoop", "Airflow", "dbt", "Snowflake",
        "BigQuery", "Databricks", "Redshift", "Tableau", "Power BI", "Excel", "ETL", "Data Analysis", "Data Visualization", "Statistics",
        "Kafka", "PostgreSQL", "MySQL", "Data Modeling", "Data Warehousing", "Jupyter", "Matplotlib"],
      words: ["data engineer", "data pipeline", "etl", "analytic", "warehouse", "data science", "data scientist", "dashboard", "reporting", "big data"],
    },
    ml: {
      skills: ["Machine Learning", "Deep Learning", "PyTorch", "TensorFlow", "Keras", "Scikit-learn", "Natural Language Processing",
        "Computer Vision", "Large Language Models", "Generative AI", "Hugging Face", "RAG", "LangChain", "LangGraph", "Fine-tuning",
        "Prompt Engineering", "MLOps", "Transformers", "OpenCV", "Python", "Pandas", "NumPy", "Artificial Intelligence",
        "Reinforcement Learning", "Neural Networks", "Vector Databases", "XGBoost", "Statistics", "Jupyter", "CUDA"],
      words: ["machine learning", "deep learning", "neural", "llm", "ml", "model training", "inference", "generative", "nlp", "computer vision"],
    },
    cloud: {
      skills: ["AWS", "GCP", "Azure", "Docker", "Kubernetes", "Terraform", "CI/CD", "Jenkins", "GitHub Actions", "Linux", "Bash",
        "Shell Scripting", "Ansible", "Prometheus", "Grafana", "Nginx", "Helm", "Serverless", "AWS Lambda", "CloudFormation"],
      words: ["cloud", "devops", "infrastructure", "deploy", "deployment", "sre", "site reliability", "container", "observability", "pipeline"],
    },
    mobile: {
      skills: ["Swift", "SwiftUI", "Kotlin", "Android", "iOS", "React Native", "Flutter", "Dart", "Objective-C", "Java", "Xcode"],
      words: ["mobile", "android", "ios", "app store"],
    },
    systems: {
      skills: ["C", "C++", "Rust", "Go", "Assembly", "Embedded Systems", "Operating Systems", "Multithreading", "Concurrency",
        "Networking", "TCP/IP", "RTOS", "Linux", "Bash", "Verilog", "FPGA", "Computer Architecture", "Python"],
      words: ["embedded", "firmware", "low level", "kernel", "real time", "operating system", "hardware", "systems programming", "driver"],
    },
    testing: {
      skills: ["Selenium", "Playwright", "Cypress", "Jest", "PyTest", "JUnit", "Unit Testing", "Test Automation", "Postman", "Integration Testing"],
      words: ["qa", "quality assurance", "test automation", "testing", "sdet"],
    },
  };
  // The areas any general software job draws on.
  const GENERAL_AREAS = ["frontend", "backend", "cloud", "systems", "testing"];
  // Fine for any software job.
  const GENERAL_SKILLS = ["Git", "GitHub", "Data Structures & Algorithms", "Object-Oriented Programming", "Object-Oriented Design",
    "Unit Testing", "Agile", "Scrum", "Jira", "Debugging", "System Design", "Software Engineering", "Software Development",
    "Problem Solving", "Version Control", "Code Review", "Linux", "Communication", "Teamwork"];

  const AREA_OF = new Map();   // skill key -> area names
  for (const [area, a] of Object.entries(SKILL_AREAS)) {
    for (const n of a.skills) for (const k of skillForms(n)) AREA_OF.set(k, [...new Set([...(AREA_OF.get(k) || []), area])]);
  }
  const GENERAL_KEYS = new Set(GENERAL_SKILLS.flatMap((n) => [...skillForms(n)]));

  function areasOf(skill) {
    const out = new Set();
    for (const k of skillForms(skill)) for (const a of AREA_OF.get(k) || []) out.add(a);
    return out;
  }

  /** A posting read once, for asking which skills it names. */
  function postingIndex(text) {
    const raw = String(text || "");
    return { raw, keys: " " + skillKey(raw) + " " };
  }

  /**
   * Whether the posting names the skill, under any of its names. One- and two-letter
   * names ("Go", "C", "AI") have to appear as written, so "go" and "a" in prose don't count.
   */
  function postingNames(index, skill) {
    const names = new Set([String(skill || "").trim()]);
    const inBrackets = String(skill || "").match(/\(([^)]+)\)/);
    if (inBrackets) names.add(inBrackets[1].trim());
    const g = SKILL_GROUP_OF.get(skillKey(skill));
    if (g != null) SKILL_GROUPS[g].forEach((n) => names.add(n));
    for (const k of skillForms(skill)) {
      if (k.length > 2 && index.keys.includes(" " + k + " ")) return true;
    }
    for (const n of names) {
      if (skillKey(n).length > 2 || !n || /^cv$/i.test(n)) continue;   // a CV is also a resume
      const esc = n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (new RegExp(`(?<![\\w.&+#-])${esc}(?![\\w&+#-])`).test(index.raw)) return true;
    }
    return false;
  }

  /**
   * The resume skills worth adding to this job's skills box: those the posting names, then
   * neighbours in the kind of work it's about (TypeScript for a React job), then the ones any
   * software job takes (Git). The rest are left out: fine-tuning has no place on an HTML/CSS job.
   * `keywords` are the ones Onward scores the job by (its checklist read from the stored
   * description); a skill they name counts as named, alongside the posting's own text.
   * Without a posting or keywords, or when they say too little to tell, every skill is kept.
   */
  function skillsForPosting(skills, posting, title, { keywords = [] } = {}) {
    const list = Array.isArray(skills) ? skills.filter(Boolean) : [];
    const kw = (Array.isArray(keywords) ? keywords : []).map((k) => (Array.isArray(k) ? k[0] : k))
      .filter((k) => typeof k === "string" && k);
    const hasPosting = String(posting || "").trim().length >= 200;
    if (!hasPosting && !kw.length) return { keep: list, left: [], known: false };
    const text = (hasPosting ? String(posting) : "") + "\n" + String(title || "") + "\n" + kw.join(", ");
    const index = postingIndex(text);
    // A skill shared by several areas (Python, SQL, Java) says nothing about which one the
    // job is, so only an area's own skills and words count towards it.
    const score = {};
    for (const [area, a] of Object.entries(SKILL_AREAS)) {
      let n = a.skills.filter((s) => areasOf(s).size === 1 && postingNames(index, s)).length;
      n += a.words.filter((w) => index.keys.includes(" " + skillKey(w) + " ")).length;
      score[area] = n;
    }
    const named = list.filter((s) => postingNames(index, s));
    let areas = Object.keys(score).filter((a) => score[a] >= 2);
    if (!areas.length) {
      // A general software posting ("Java, Python, C++ or JavaScript") takes general software
      // skills; the specialist ones (ML, data, mobile) only when it touches on them.
      const touched = Object.keys(score).filter((a) => score[a] >= 1);
      if (!touched.length && !named.length) return { keep: list, left: [], known: false };
      areas = [...new Set([...GENERAL_AREAS, ...touched])];
    }
    const near = list.filter((s) => !named.includes(s)
      && ([...skillForms(s)].some((k) => GENERAL_KEYS.has(k)) || [...areasOf(s)].some((a) => areas.includes(a))));
    return { keep: [...named, ...near], left: list.filter((s) => !named.includes(s) && !near.includes(s)), known: true, areas };
  }

  root.JR = root.JR || {};
  root.JR.rules = {
    PROFILE_FIELDS, STANDARD_ANSWERS, EEO_ANSWERS, DECLINE, DEFAULT_ANSWERS, DEFAULT_PROFILE, DEFAULT_SETTINGS,
    US_STATES, CHOICE_KINDS, TEXT_KINDS, clean, normQ, classify, place, answerFor, pickOption, asText, alternatives, DECLINE_RE, historyField,
    companyKey, sameCompany, companyNames, namesIn, mentions, savedAnswerUse, languageList, relevantEntries, skillKey, sameSkill, splitSkills, skillSearchTerms, skillsForPosting,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
