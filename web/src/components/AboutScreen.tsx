export default function AboutScreen() {
  return (
    <div className="about">
      <div className="screen-title" style={{ marginBottom: 18 }}>
        About
      </div>

      <p className="lead">
        The Iccuthologist turns a running Phish setlist-prediction model into something you
        can actually browse before a show. Pick a tour to see which songs are due, select a
        run of nights to get the joint odds of hearing each song, or read a full proposed
        setlist night by night. Everything on this site is the output of one statistical
        engine — here's how it works.
      </p>

      <div className="step-kicker">01 — The data</div>
      <h3>Every Phish show since 1983</h3>
      <p>
        The engine ingests the complete performance history from{" "}
        <a href="https://phish.net" target="_blank" rel="noreferrer" className="mono-inline">
          Phish.net
        </a>{" "}
        — every show, setlist, song, and venue. From that history it builds a feature frame
        for each song at each show, capturing the signals that actually drive rotation:
      </p>
      <ul>
        <li>
          <strong>Recency &amp; gaps</strong> — how many shows since the song last appeared,
          relative to its own typical gap (a song "due" by its own standard stands out).
        </li>
        <li>
          <strong>Decayed play rate</strong> — a half-life-weighted frequency, so recent
          seasons count more than a burst in 1997.
        </li>
        <li>
          <strong>Run &amp; venue context</strong> — whether it was played earlier in the
          same multi-night run or the last time the band was at this venue.
        </li>
        <li>
          <strong>Era &amp; tenure</strong> — the song's rate within the current era and how
          long it's been in the active repertoire.
        </li>
      </ul>
      <p>
        Crucially, every feature for a given show is computed using <strong>only</strong>{" "}
        shows that came before it — no peeking at the future. That's what makes the backtest
        an honest measure of how the model would have done in real time.
      </p>

      <div className="step-kicker">02 — The models</div>
      <h3>From features to calibrated probabilities</h3>
      <p>
        Three models turn those features into a per-song probability of being played:
      </p>
      <ul>
        <li>
          <strong>Heuristic</strong> — a transparent multiplicative baseline (decayed rate,
          adjusted by the "due", run, and venue signals). It's the default because you can
          read exactly why a song scored the way it did.
        </li>
        <li>
          <strong>Logistic regression &amp; gradient boosting</strong> — learned models,
          each calibrated so a stated 30% really means ~30% over the long run. LR wins the
          backtest on Brier score and log-loss.
        </li>
      </ul>
      <p>
        A show isn't an independent coin-flip per song — a night has a roughly fixed length.
        So each show's raw scores are renormalized to sum to <strong>K</strong>, the typical
        number of distinct songs in a show for that era. The result is a calibrated
        probability for every candidate song, on every upcoming night.
      </p>

      <div className="step-kicker">03 — The simulator</div>
      <h3>Playing the tour out, thousands of times</h3>
      <p>
        Single-night probabilities can't answer "will I hear Hood at least once across these
        three nights?" — because the nights are coupled: a song played Friday is far less
        likely Saturday, and no-repeat rules bind within a run. So the engine runs a{" "}
        <strong>forward Monte-Carlo simulation</strong>: it samples a plausible setlist for
        the next night, folds that result back into the model's state, re-scores the night
        after, and walks to the end of the horizon — then repeats the whole tour a couple
        thousand times.
      </p>
      <p>
        Those raw simulations are the source of truth. Because they already encode the full
        <em> joint</em> behavior over the tour, any question you ask — an arbitrary set of
        nights, a specific song's next appearance, a whole-tour tally — is an exact count
        over the same samples. Simulate once; reduce it many ways.
      </p>

      <div className="step-kicker">04 — What you see</div>
      <h3>Reading the numbers</h3>
      <ul>
        <li>
          <strong>Tours</strong> — for each song, its most likely number of plays and how
          confident we are, the odds of at least one play, and a lock / likely / bustout /
          longshot bucket. A song is played a whole number of times, so we report the most
          likely count and its probability rather than a fractional average.
        </li>
        <li>
          <strong>Shows → Run view</strong> — pick any set of nights and get the true joint
          probability of hearing each song at least once across them (a union over the
          simulations, not naive independent-events math), plus the single most likely night.
        </li>
        <li>
          <strong>Shows → Proposed setlist</strong> — a full, ordered, plausible setlist for
          one night: songs drawn into set/encore slots by where they historically land, with
          real segue pairs (Tweezer → Tweezer Reprise) kept intact.
        </li>
      </ul>

      <div className="step-kicker">05 — Freshness &amp; honesty</div>
      <h3>Recomputed after every show, and upfront about limits</h3>
      <p>
        Predictions are a batch artifact: they're recomputed whenever the underlying state
        changes — a show gets played, the schedule shifts, or the model itself changes — and
        served as fast static reads in between. So the moment last night's setlist posts, the
        next night's odds already reflect it.
      </p>
      <p>
        And the honest part: predicting the exact ordered setlist of an improvising band has
        a hard ceiling — the setlist view trades pinpoint accuracy for realistic variety, and
        the song-level odds are calibrated but never certainties. It's a well-informed forecast,
        not an oracle.
      </p>

      <div className="about-blurb">
        <div className="label-caps">From the creator</div>
        <p>
          [ Your blurb goes here — the story behind the project, why you built it, whatever
          you want folks to know. Replace this placeholder in AboutScreen.tsx. ]
        </p>
      </div>

      <div className="card" style={{ padding: "20px 24px" }}>
        <div className="attribution">
          Show data, setlists, and song histories used to build these predictions come from{" "}
          <a href="https://phish.net" target="_blank" rel="noreferrer">
            Phish.net
          </a>{" "}
          — an invaluable community-run archive of Phish setlists, jam charts, and show
          history going back to 1983. Go check it out.
        </div>
      </div>
    </div>
  );
}
