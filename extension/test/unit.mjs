// Unit tests for src/shared/rules.js: question classification and option
// matching, using wording seen on live Greenhouse, Lever, Ashby and Workday forms.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

vm.runInThisContext(readFileSync(new URL("../src/shared/rules.js", import.meta.url), "utf8"));
const R = globalThis.JR.rules;

let failed = 0, passed = 0;
function check(name, fn) {
  try { fn(); passed++; } catch (e) { failed++; console.log("FAIL", name, "\n  ", e.message.split("\n").join("\n   ")); }
}

const C = (label, kind = "text", attrs = "", section = "", options = null) => R.classify(label, kind, attrs, section, options);
const is = (label, kind, want, attrs, section) => check(`${kind} "${label}"`, () => {
  const got = C(label, kind, attrs, section);
  for (const [k, v] of Object.entries(want)) assert.equal(got[k], v, `${label}: ${k} = ${got[k]}, want ${v} (${JSON.stringify(got)})`);
});

// Profile fields
is("First Name", "text", { cat: "profile", key: "firstName" });
is("Last Name", "text", { cat: "profile", key: "lastName" });
is("Preferred First Name", "text", { cat: "profile", key: "preferredName" });
is("Full name", "text", { cat: "profile", key: "fullName" });
is("Name", "text", { cat: "profile", key: "fullName" }, "_systemfield_name");
is("Email", "email", { cat: "profile", key: "email" });
is("Phone", "tel", { cat: "profile", key: "phone" });
is("Phone Number", "tel", { cat: "profile", key: "phone" });
is("LinkedIn Profile", "text", { cat: "profile", key: "linkedin" });
is("LinkedIn URL", "text", { cat: "profile", key: "linkedin" }, "urls[LinkedIn]");
is("GitHub URL", "text", { cat: "profile", key: "github" });
is("Website", "text", { cat: "profile", key: "portfolio" });
is("Current location", "text", { cat: "profile", key: "location" });
is("Location (City)", "combo", { cat: "profile", key: "location", transform: undefined });
is("City", "text", { cat: "profile", key: "location", transform: "city" });
is("Country", "rselect", { cat: "profile", key: "country" });
is("Current company", "text", { cat: "profile", key: "company" });
is("School", "rselect", { cat: "profile", key: "school" });
is("Degree", "rselect", { cat: "profile", key: "degree" });
is("Discipline", "rselect", { cat: "profile", key: "major" });
is("Graduation Year", "select", { cat: "profile", key: "gradDate", transform: "year" });
is("", "text", { cat: "profile", key: "fullName" }, "name");
is("", "text", { cat: "profile", key: "company" }, "org");
is("First Name", "text", { cat: "profile", key: "firstName" }, "legalNameSection_firstName");

// Not the applicant
is("Emergency contact first name", "text", { cat: "custom" });
is("Name of the employee who referred you", "text", { cat: "custom" });

// Sponsorship and work authorization: never guessed, always mapped
is("Are you currently eligible to work legally in the United States of America?", "rselect", { cat: "std", key: "workAuth" });
is("Are you legally authorized to work in the United States?", "radio", { cat: "std", key: "workAuth" });
is("Do you now or in the future require immigration support or visa sponsorship to continue working in the United States?", "rselect", { cat: "std", key: "sponsorAny" });
is("Do you now, or in the future require sponsorship?", "radio", { cat: "std", key: "sponsorAny" });
is("Will you now or in the future require sponsorship for employment visa status in the United States?", "yesno", { cat: "std", key: "sponsorAny" });
is("Will you require visa sponsorship to start working?", "select", { cat: "std", key: "sponsorNow" });
is("Will you in the future require sponsorship?", "select", { cat: "std", key: "sponsorFuture" });
is("Do you have the unrestricted right to work in the United States?", "radio", { cat: "std", key: "authUnrestricted" });
is("Are you authorized to work in the US without sponsorship?", "radio", { cat: "std", key: "authUnrestricted" });
is("What is your current visa status?", "text", { cat: "std", key: "workAuthText" });
is("Please describe your sponsorship needs", "textarea", { cat: "std", key: "sponsorshipText" });
is("Will you require sponsorship?", "text", { cat: "std", key: "sponsorAny", mode: "yesno" });
is("Are you a U.S. citizen?", "radio", { cat: "std", key: "citizen" });
is("Are you willing to relocate?", "radio", { cat: "std", key: "relocate" });
is("Are you at least 18 years of age?", "radio", { cat: "std", key: "over18" });
is("How did you hear about this job?", "text", { cat: "std", key: "hearAbout" });
is("How Did You Hear About Us?", "prompt", { cat: "std", key: "hearAbout" });

// EEO
is("I identify my gender as:", "rselect", { cat: "eeo", key: "eeoGender" });
is("Gender", "select", { cat: "eeo", key: "eeoGender" });
is("I identify my race/ethnicity as (please mark all that apply):", "rselect", { cat: "eeo", key: "eeoRace" });
is("Are you Hispanic/Latino?", "select", { cat: "eeo", key: "eeoHispanic" });
is("Veteran Status:", "rselect", { cat: "eeo", key: "eeoVeteran" });
is("I have a disability:", "rselect", { cat: "eeo", key: "eeoDisability" });
is("I identify my sexual orientation as:", "rselect", { cat: "eeo", key: "eeoOrientation" });
is("Do you identify as transgender?", "rselect", { cat: "eeo", key: "eeoTransgender" });
is("I identify as:", "rselect", { cat: "never" }, "", "Voluntary Self-Identification");
is("Do you identify as part of the LGBTQ+ community?", "rselect", { cat: "eeo", key: "eeoOrientation" });
check("location spellings", () => {
  const alts = R.alternatives({ cat: "profile", key: "location" }, "Boston, MA");
  assert.equal(alts.map((a) => R.pickOption(a, ["Boston, Lincolnshire, United Kingdom", "Boston, Massachusetts, United States"])).find((i) => i >= 0), 1);
});

// Never filled
is("Do you acknowledge that this is a hybrid role based in San Francisco and you will be required to come into the office four days a week?", "rselect", { cat: "never" });
is("By checking this box, I consent to Chime collecting, storing, and processing my responses to the demographic data surveys above.", "checkbox", { cat: "never" });
is("What are your salary expectations?", "text", { cat: "never" });
is("Pronouns", "text", { cat: "never" });
is("Signature", "text", { cat: "never" });
is("Middle Name", "text", { cat: "never" }, "legalNameSection_middleName");
is("Phone Extension", "text", { cat: "never" });
is("Address Line 2", "text", { cat: "never" });
is("Apt / Suite", "text", { cat: "never" });

// Workday's required My Information fields (real wording, Amentum and RSM)
is("Address Line 1", "text", { cat: "profile", key: "address", transform: undefined }, "address--addressLine1 addressLine1");
is("Street Address", "text", { cat: "profile", key: "address" });
is("Address", "text", { cat: "profile", key: "address", transform: "full" });
is("Email Address", "email", { cat: "profile", key: "email" });
is("Current Address", "text", { cat: "profile", key: "location" });
is("Country Phone Code", "prompt", { cat: "profile", key: "country" }, "phoneNumber--countryPhoneCode");
is("Country / Territory Phone Code", "prompt", { cat: "profile", key: "country" });
is("Phone Device Type", "listbutton", { cat: "profile", key: "phoneType" }, "phoneNumber--phoneType phoneType");
is("Phone Number", "text", { cat: "profile", key: "phone" }, "phoneNumber--phoneNumber phoneNumber");
is("State", "listbutton", { cat: "profile", key: "location", transform: "state" }, "address--countryRegion countryRegion");
is("Postal Code", "text", { cat: "profile", key: "postalCode" });
is("How Did You Hear About Us?", "prompt", { cat: "std", key: "hearAbout" }, "source--source");
check("full address from the profile", () => assert.equal(
  R.answerFor({ cat: "profile", key: "address", transform: "full" }, { address: "1 Test St", location: "Boston, MA", postalCode: "02115" }),
  "1 Test St, Boston, MA 02115"));
check("phone type defaults to Mobile", () => assert.equal(R.DEFAULT_PROFILE.phoneType, "Mobile"));
check("phone type picks Workday's Mobile", () => {
  const alts = R.alternatives({ key: "phoneType" }, "Mobile");
  assert.equal(["Fax", "Mobile", "Pager", "Telephone"][alts.map((a) => R.pickOption(a, ["Fax", "Mobile", "Pager", "Telephone"])).find((i) => i >= 0)], "Mobile");
});
check("phone code picks the country's entry", () => {
  const opts = ["United States Minor Outlying Islands (+1)", "United States of America (+1)", "India (+91)"];
  const alts = R.alternatives({ key: "country" }, "United States");
  const i = alts.map((a) => R.pickOption(a, opts)).find((x) => x >= 0);
  assert.equal(opts[i], "United States of America (+1)");
});
// Degree lists seen on real Workday sites.
const pickDegree = (degree, opts) => {
  for (const a of R.alternatives({ key: "degree" }, degree)) { const i = R.pickOption(a, opts); if (i >= 0) return opts[i]; }
  return null;
};
const RSM_DEGREES = ["Not Completed", "GED", "High School", "AA", "AS", "Bachelor's", "MA", "MS", "MBA", "PhD", "JD", "LLM"];
check("degree: master of science is MS", () => assert.equal(pickDegree("Master of Science in Computer Engineering", RSM_DEGREES), "MS"));
check("degree: bachelor of engineering is Bachelor's", () => assert.equal(pickDegree("Bachelor of Engineering", RSM_DEGREES), "Bachelor's"));
check("degree: M.S. spelled with dots", () => assert.equal(pickDegree("Master of Science", ["B.S.", "M.S.", "Ph.D."]), "M.S."));
check("degree: arts is not science", () => assert.equal(pickDegree("Master of Arts", RSM_DEGREES), "MA"));
check("degree: MBA", () => assert.equal(pickDegree("MBA", RSM_DEGREES), "MBA"));

// Skills as Workday's skills list names them (seen on a live tenant, October 2026).
for (const [mine, site] of [["Java", "Java (Programming Language)"], ["C++", "C++ Programming Language"], ["RAG pipelines", "RAG Pipeline"],
  ["AWS", "Amazon Web Services (AWS)"], ["GCP", "Google Cloud Platform (GCP)"], ["Data Structures & Algorithms", "Data Structures and Algorithms"],
  ["Node.js", "Node JS"], ["Node.js", "NodeJS"], ["React.js", "React (JavaScript Library)"], ["REST APIs", "RESTful API"],
  ["Scikit-learn", "Scikit-Learn"], ["PostgreSQL", "PostgreSQL (Database)"], ["CI/CD", "Continuous Integration"], ["C", "C Programming Language"]]) {
  check(`skill: ${mine} is ${site}`, () => assert.ok(R.sameSkill(mine, site)));
}
for (const [mine, site] of [["Java", "JavaScript"], ["C", "C++"], ["SQL", "MySQL"], ["Git", "GitHub"], ["Go", "Google"], ["React.js", "React Native"]]) {
  check(`skill: ${mine} is not ${site}`, () => assert.ok(!R.sameSkill(mine, site)));
}
check("skills: HTML5/CSS3 is two, CI/CD one", () => assert.deepEqual(R.splitSkills(["HTML5/CSS3", "CI/CD", "Java"]), ["HTML5", "CSS3", "CI/CD", "Java"]));
check("skills: searched by other names too", () => {
  assert.deepEqual(R.skillSearchTerms("RAG pipelines"), ["RAG pipelines", "RAG pipeline", "RAG"]);
  assert.deepEqual(R.skillSearchTerms("AWS"), ["AWS", "Amazon Web Services"]);
});

// Skills that fit the posting
const MY_SKILLS = ["Python", "JavaScript", "TypeScript", "React", "HTML", "CSS", "Node.js", "SQL", "PyTorch", "Fine-tuning",
  "LangChain", "Docker", "AWS", "Git", "C++", "Go", "Tableau"];
const pad = (s) => s + " We are an equal opportunity employer and value a diverse team. Benefits include health insurance, a 401k and paid leave.";
const FRONTEND = pad("We are hiring a Frontend Engineer to build our customer web app. You will write HTML, CSS and JavaScript, build responsive user interfaces in the browser, and work with designers.");
const ML = pad("Join our machine learning team to train and fine-tune large language models. Experience with PyTorch, Hugging Face and deep learning is required; Python is a must. You will run inference at scale.");
const BACKEND = pad("Backend engineer for our payments API. You'll design microservices in Java and Go, own PostgreSQL schemas and REST APIs, and run them on AWS with Docker. Distributed systems experience is a plus.");
check("fit skills: HTML/CSS job takes web skills, not fine-tuning", () => {
  const r = R.skillsForPosting(MY_SKILLS, FRONTEND, "Frontend Engineer");
  for (const s of ["HTML", "CSS", "JavaScript", "TypeScript", "React", "Git"]) assert.ok(r.keep.includes(s), s);
  for (const s of ["PyTorch", "Fine-tuning", "LangChain", "Tableau", "C++", "Go"]) assert.ok(r.left.includes(s), s);
  assert.deepEqual(r.keep.slice(0, 3), ["JavaScript", "HTML", "CSS"], "the posting's own skills come first");
});
check("fit skills: ML job takes ML skills, not CSS", () => {
  const r = R.skillsForPosting(MY_SKILLS, ML, "Machine Learning Engineer");
  for (const s of ["Python", "PyTorch", "Fine-tuning", "LangChain", "Git"]) assert.ok(r.keep.includes(s), s);
  for (const s of ["HTML", "CSS", "React", "Tableau"]) assert.ok(r.left.includes(s), s);
});
check("fit skills: backend job", () => {
  const r = R.skillsForPosting(MY_SKILLS, BACKEND, "Software Engineer, Backend");
  for (const s of ["Go", "Docker", "AWS", "SQL", "Node.js", "Python", "Git"]) assert.ok(r.keep.includes(s), s);
  for (const s of ["HTML", "CSS", "Fine-tuning", "PyTorch"]) assert.ok(r.left.includes(s), s);
});
const GENERAL = pad("Develop, test and maintain software applications under the guidance of senior engineers. Participate in code reviews. Basic understanding of programming languages such as Java, Python, C++, or JavaScript. Familiarity with data structures, algorithms, and object-oriented programming. Knowledge of version control systems (e.g., Git).");
check("fit skills: general software job takes general software skills, not ML", () => {
  const r = R.skillsForPosting(MY_SKILLS, GENERAL, "Software Engineer Junior");
  for (const s of ["Python", "JavaScript", "C++", "Git", "React", "Node.js", "SQL", "Docker", "Go"]) assert.ok(r.keep.includes(s), s);
  for (const s of ["PyTorch", "Fine-tuning", "LangChain", "Tableau"]) assert.ok(r.left.includes(s), s);
});
check("fit skills: Onward's keywords alone are enough", () => {
  const kw = [["HTML/CSS", 1.0, "req"], ["React", 1.0, "req"], ["Figma", 0.5, "pref"]];
  const r = R.skillsForPosting(MY_SKILLS, "", "UI Engineer", { keywords: kw });
  assert.equal(r.known, true);
  for (const s of ["HTML", "CSS", "React", "JavaScript", "TypeScript", "Git"]) assert.ok(r.keep.includes(s), s);
  for (const s of ["Fine-tuning", "PyTorch", "Tableau"]) assert.ok(r.left.includes(s), s);
});
check("fit skills: Onward's keywords add to what the posting's text names", () => {
  // The page shows a short teaser; the app read the full description, which asks for Tableau.
  const r = R.skillsForPosting(MY_SKILLS, GENERAL, "Software Engineer Junior", { keywords: [["Tableau", 0.5, "pref"]] });
  assert.ok(r.keep.includes("Tableau"));
  assert.ok(r.left.includes("Fine-tuning"));
});
check("fit skills: short names only as written", () => {
  const r = R.skillsForPosting(["Go", "C", "R"], pad("Please go ahead and send a CV. You will have a role in a team of about 8 people doing customer support. R&D is next door."), "Support Associate");
  assert.deepEqual(r.keep, ["Go", "C", "R"], "nothing to go on, so all are kept");
  assert.equal(r.known, false);
});
check("fit skills: no posting keeps everything", () => {
  assert.deepEqual(R.skillsForPosting(MY_SKILLS, "", "Engineer").keep, MY_SKILLS);
  assert.deepEqual(R.skillsForPosting(MY_SKILLS, "Short.", "Engineer").left, []);
});
check("fit skills: site names and abbreviations count as named", () => {
  const r = R.skillsForPosting(["Amazon Web Services", "Kubernetes", "C#"], pad("Our cloud platform team runs AWS and K8s. Services are written in C# on .NET with CI/CD pipelines."), "Cloud Engineer");
  assert.deepEqual(r.keep, ["Amazon Web Services", "Kubernetes", "C#"]);
});

// Files
is("Resume/CV", "file", { cat: "file", key: "resume" });
is("Attach", "file", { cat: "file", key: "resume" }, "resume");
is("Cover Letter", "file", { cat: "never" }, "cover_letter");
is("Upload a file", "file", { cat: "file", key: "resume" }, "", "Resume/CV");
is("Transcript", "file", { cat: "never" }, "", "Resume and documents");

// Custom (remembered) and writing questions
is("Are you currently subject to any agreement with a former employer that may limit your ability to perform the duties?", "rselect", { cat: "custom", writing: false });
is("When can you start a new role?", "text", { cat: "custom" });
is("How many years of industry experience do you have?", "number", { cat: "custom" });
is("Why do you want to work at Netic?", "textarea", { cat: "custom", writing: true });
is("If yes, briefly describe what it did and what you built it with.", "textarea", { cat: "custom", writing: true });
is("Additional Information", "textarea", { cat: "custom", writing: true });
is("Can you share a link to a GitHub repo, portfolio, or writeup showing something you've built?", "text", { cat: "custom", writing: false });
is("Have you personally built and deployed a working AI agent using a framework or SDK?", "radio", { cat: "custom" });

// Palantir (Lever) wording
is("Language Skill(s) (Check all that apply)", "checkbox", { cat: "profile", key: "languages" });
is("Which programming languages do you use most?", "text", { cat: "custom" });
is("Which languages do you use most?", "text", { cat: "custom" });
is("Languages", "checkbox", { cat: "profile", key: "languages" });
is("What languages do you speak fluently?", "text", { cat: "profile", key: "languages" });
is("Preferred Name | What would you like us to call you?", "text", { cat: "profile", key: "preferredName" });
is("Which university are you currently attending or did you last attend? Please select \"Other (School Not Listed)\" if your school isn't listed.", "select", { cat: "profile", key: "school" });
is("Please include your intended graduation year for the degree or relevant learning program that you are currently enrolled in.", "select", { cat: "profile", key: "gradDate", transform: "year" });
is("Year of High School Graduation", "select", { cat: "custom" });
is("High School Name", "text", { cat: "custom" });
is("Name Pronunciation | How do you pronounce your name?", "text", { cat: "custom" });
check("preferred name falls back to first name", () => assert.equal(R.answerFor({ cat: "profile", key: "preferredName" }, { firstName: "Testy" }, {}), "Testy"));
check("languages split", () => assert.deepEqual(R.alternatives({ cat: "profile", key: "languages" }, "English, Hindi"), ["English", "Hindi"]));
check("English by default", () => assert.equal(R.DEFAULT_PROFILE.languages, "English"));

// Work history fields: grouped and filled from the resume, never sent to the AI
const hf = (label, attrs, want) => check(`historyField ${label || attrs}`, () => assert.equal(R.historyField(label, attrs), want));
hf("Job Title", "workExperience-2--jobTitle", "title");
hf("Company", "workExperience-2--companyName companyName", "company");
hf("Role Description", "workExperience-1--roleDescription roleDescription", "description");
hf("I currently work here", "workExperience-1--currentlyWorkHere currentlyWorkHere", "current");
hf("Month", "workExperience-1--startDate-dateSectionMonth-input dateSectionMonth-input startDate", "startMonth");
hf("Year", "workExperience-1--endDate-dateSectionYear-input dateSectionYear-input endDate", "endYear");
hf("From", "education-1--firstYearAttended-dateSectionYear-input dateSectionYear-input firstYearAttended", "startYear");
hf("Field of Study", "education-1--fieldOfStudy searchBox fieldOfStudy", "field");
hf("Overall Result (GPA)", "education-1--gradeAverage gradeAverage", "gpa");
hf("Start date month", "start-date-month--0", "startMonth");
hf("Title", "title--0", "title");
hf("Current role", "current-role--0", "current");
hf("What is your GPA?", "question_77", "gpa");
hf("Role description", "", "description");
hf("Why do you want to work at our company?", "question_12", null);
hf("Describe a project you're proud of", "question_13", null);
hf("How many years of experience do you have?", "cards[a][field2]", null);
hf("LinkedIn", "socialNetworkAccounts--linkedInAccount linkedinQuestion", null);

// Saved answers and the company they were written for
const ELEVEN = "I want to join ElevenLabs because ElevenMusic is the most fun audio product I have used, and I built TypeScript tools.";
check("names: company and its product, not a skill", () => assert.deepEqual(
  R.namesIn(ELEVEN, "ElevenLabs", "Software Engineer, ElevenMusic", "ElevenMusic lets anyone compose. We use TypeScript."), ["ElevenLabs", "ElevenMusic"]));
check("names: suffixes and short forms", () => assert.deepEqual(R.companyNames("Chime Financial, Inc"), ["Chime Financial", "Chime"]));
check("names: neutral answer names nothing", () => assert.deepEqual(R.namesIn("I like building reliable APIs in Go.", "ElevenLabs", "", "We use Go."), []));
check("same company across spellings", () => assert.ok(R.sameCompany("ElevenLabs", "Elevenlabs, Inc.") && !R.sameCompany("ElevenLabs", "Chime")));
check("saved answer: same company is offered", () => assert.equal(R.savedAnswerUse({ a: ELEVEN, company: "ElevenLabs" }, "ElevenLabs Inc").use, "use"));
check("saved answer: other company is warned", () => {
  const u = R.savedAnswerUse({ a: ELEVEN, company: "ElevenLabs", names: ["ElevenLabs", "ElevenMusic"] }, "Mock Co");
  assert.equal(u.use, "warn"); assert.deepEqual(u.names, ["ElevenLabs", "ElevenMusic"]);
});
check("saved answer: old entries without names still caught", () =>
  assert.deepEqual(R.savedAnswerUse({ a: ELEVEN, company: "ElevenLabs" }, "Mock Co").names, ["ElevenLabs", "ElevenMusic"]));
check("saved answer: neutral one is offered everywhere", () =>
  assert.equal(R.savedAnswerUse({ a: "Two weeks after an offer", company: "ElevenLabs" }, "Mock Co").use, "use"));

// Languages with levels
check("languages: levels", () => assert.deepEqual(R.languageList("English (Fluent), Hindi, Gujarati - Native, Serbo-Croatian"), [
  { name: "English", level: "Fluent" }, { name: "Hindi", level: "" }, { name: "Gujarati", level: "Native" }, { name: "Serbo-Croatian", level: "" }]));
check("languages: names for check-all-that-apply", () => assert.deepEqual(R.alternatives({ key: "languages" }, "English (Fluent), Hindi"), ["English", "Hindi"]));

// Only entries related to the job go to the AI
const HIST = {
  work: [{ title: "Full Stack Developer Intern", company: "Example Labs", description: "Built Vue.js screens with PouchDB.\nExtended Node.js REST endpoints." },
    { title: "Barista", company: "Cafe", description: "Made coffee." }],
  projects: [{ name: "Turbofan RUL", tech: "Python, PyTorch", description: "Trained an LSTM on NASA sensor data." },
    { name: "Portfolio Manager", tech: "Java Swing", description: "Desktop app for stock portfolios." }],
};
check("entries: a web job gets the web work, not the ML project", () => {
  const t = R.relevantEntries(HIST, "We use Vue.js, Node.js and REST APIs.", "Frontend Engineer");
  assert.ok(t.includes("Example Labs") && !t.includes("Turbofan") && !t.includes("Barista") && !t.includes("Portfolio"), t);
});
check("entries: an ML job gets the ML project only", () => {
  const t = R.relevantEntries(HIST, "Train PyTorch LSTM models on sensor data in Python.", "ML Engineer");
  assert.ok(t.includes("Turbofan") && !t.includes("Example Labs") && !t.includes("Portfolio"), t);
});
check("entries: nothing related, nothing sent", () => assert.equal(R.relevantEntries(HIST, "Sell insurance door to door.", "Sales Associate"), ""));

// Projects written into one box (history.js needs no DOM for this)
globalThis.JR.dom = {};
globalThis.JR.widgets = {};
vm.runInThisContext(readFileSync(new URL("../src/content/history.js", import.meta.url), "utf8"));
const PROJ = [
  { name: "Turbofan Life Estimation", tech: "Python, PyTorch", start: { month: 2, year: 2025 }, end: { month: 4, year: 2025 },
    description: "Trained an LSTM on NASA C-MAPSS.\nBeat prior RMSE.", link: "https://example.test/rul" },
  { name: "Portfolio Manager", role: "Full Stack Developer", current: true, start: { month: 3, year: 2024 }, bullets: ["Java Swing app."] },
];
check("projects text: one block per project", () => assert.equal(JR.history.projectsText(PROJ, 0),
  "Turbofan Life Estimation (Python, PyTorch), Feb 2025 – Apr 2025\nTrained an LSTM on NASA C-MAPSS.\nBeat prior RMSE.\nhttps://example.test/rul\n\n" +
  "Portfolio Manager, Full Stack Developer, Mar 2024 – present\nJava Swing app."));
check("projects text: whole projects within the limit", () => assert.ok(!JR.history.projectsText(PROJ, 130).includes("Portfolio")));
check("projects text: one line", () => assert.ok(!JR.history.projectsText(PROJ, 0, true).includes("\n")));

// Workday's self-identify page and other generic demographic labels
const DIS = ["Yes, I have a disability (or previously had a disability)", "No, I do not have a disability and have not had one in the past", "I do not want to answer"];
check("eeo by options: generic label", () => assert.equal(C("Please check one of the boxes below:", "checkbox", "", "Self Identify", DIS).key, "eeoDisability"));
is("Voluntary Self-Identification of Disability", "checkbox", { cat: "eeo", key: "eeoDisability" });
is("Please select the veteran status which most accurately describes your relationship with the military", "listbutton", { cat: "eeo", key: "eeoVeteran" });
check("eeo by options: gender", () => assert.equal(C("Please select one", "radio", "", "", ["Male", "Female", "Not Declared"]).key, "eeoGender"));
is("By checking this box I consent to the processing of my disability information", "checkbox", { cat: "never" });
is("Name", "text", { cat: "profile", key: "fullName" }, "", "Self Identify");
check("self-identify date parts are today", () => {
  const c = C("Month", "text", "selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input dateSectionMonth-input dateSignedOn", "Self Identify");
  assert.equal(c.key, "today"); assert.equal(c.transform, "month");
  assert.equal(R.answerFor(c, {}, {}), String(new Date().getMonth() + 1).padStart(2, "0"));
});
is("Date", "text", { cat: "custom" }, "", "Additional Information");

// Option matching
const pick = (v, opts, want) => check(`pick ${v} from ${opts.join("|")}`, () => assert.equal(R.pickOption(v, opts), want));
pick("yes", ["No", "Yes"], 1);
pick("decline", ["Male", "Female", "Not Declared"], 2);
pick("decline", DIS, 2);
pick("no", ["Yes, I will require sponsorship", "No, I will not require sponsorship"], 1);
pick("yes", ["Yes - now", "Yes - in the future", "No"], -1);
pick("yes", ["I am authorized to work for any employer", "I will need sponsorship"], -1);
pick("decline", ["Male", "Female", "Decline To Self Identify"], 2);
pick("decline", ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"], 2);
pick("decline", ["I am a protected veteran", "I am not a protected veteran", "I prefer not to answer"], 2);
pick("decline", ["Male", "Female"], -1);
pick("Asian", ["Hispanic or Latino", "Asian (Not Hispanic or Latino)", "White (Not Hispanic or Latino)"], 1);
pick("United States", ["Canada", "United States", "United States Minor Outlying Islands"], 1);
pick("LinkedIn", ["Job board", "LinkedIn", "Referral"], 1);
pick("Male", ["Male", "Female"], 0);
check("country aliases", () => {
  const opts = ["Canada", "United States of America", "United States Minor Outlying Islands"];
  const alts = R.alternatives({ key: "country" }, "United States");
  assert.equal(alts.map((a) => R.pickOption(a, opts)).find((i) => i >= 0), 1);
});

// Answers
const P = { firstName: "Testy", lastName: "McTest", location: "Boston, MA", gradDate: "May 2026" };
check("full name", () => assert.equal(R.answerFor({ cat: "profile", key: "fullName" }, P, {}), "Testy McTest"));
check("city", () => assert.equal(R.answerFor({ cat: "profile", key: "location", transform: "city" }, P, {}), "Boston"));
check("state", () => assert.equal(R.answerFor({ cat: "profile", key: "location", transform: "state" }, P, {}), "Massachusetts"));
check("grad year", () => assert.equal(R.answerFor({ cat: "profile", key: "gradDate", transform: "year" }, P, {}), "2026"));
check("sponsor any: one yes", () => assert.equal(R.answerFor({ cat: "std", key: "sponsorAny" }, {}, { sponsorNow: "no", sponsorFuture: "yes" }), "yes"));
check("sponsor any: both no", () => assert.equal(R.answerFor({ cat: "std", key: "sponsorAny" }, {}, { sponsorNow: "no", sponsorFuture: "no" }), "no"));
check("sponsor any: unknown", () => assert.equal(R.answerFor({ cat: "std", key: "sponsorAny" }, {}, { sponsorNow: "no", sponsorFuture: "" }), ""));
check("no saved sponsorship answer by default", () => assert.equal(R.answerFor({ cat: "std", key: "sponsorAny" }, {}, R.DEFAULT_ANSWERS), ""));
check("EEO defaults to decline", () => assert.equal(R.DEFAULT_ANSWERS.eeoGender, R.DECLINE));
check("normQ", () => assert.equal(R.normQ("  Why do you want to work at Netic?* (Required) "), "why do you want to work at netic"));

console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
