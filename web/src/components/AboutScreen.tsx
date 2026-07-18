export default function AboutScreen() {
  return (
    <div className="about">
      <div className="screen-title" style={{ marginBottom: 18 }}>
        About
      </div>

      <p className="lead">
        The Iccuthologist is a live experiment in predictive modelling of Phish setlists:
        as much about the limitations of this project as its possibilities. Pick a tour to
        see which songs are due, select a run of nights to get the joint odds of hearing
        each song, or read a full proposed setlist night by night. Under the hood there are
        two layers: a statistical engine powering the tour math and simulations, and a
        roster of frontier LLMs that research each show and submit their own predictions,
        scored head-to-head against the engine's baseline. Here's how both work.
      </p>

      <div className="about-blurb">
        <div className="label-caps">From the co-creator</div>
        <p>
          In the lot before my{" "}
          <a
            href="https://phish.net/setlists/phish-august-13-2009-darien-lake-performing-arts-center-darien-center-ny-usa.html"
            target="_blank"
            rel="noreferrer"
          >
            first Phish show
          </a>
          , someone asked me what I thought they'd open with. I had no goddamn idea.
        </p>
        <p>
          Fast-forward to July 2026, I'm about to head to Deer Creek for shows 7-10, and I
          think to myself: what if I point the current state-of-the-art frontier AI at the
          phish.net dataset? 24 hours later, I still had no real idea what they're going to
          play. But as with most events that aren't planned, it's fun to speculate.
        </p>
        <p>
          After the run, I realized the real fun of this project. We have verifiable
          statistical propensities on the one hand, and the infinite variability of a band
          that never repeats a setlist on the other. None of their choices are random, but
          they're profoundly aleatoric. Every night of a tour, they deal us approximately
          18 cards from a deck of a thousand-plus, and you never know when you're gonna
          catch that elusive first-time-played. Then they've shown their proverbial hand,
          and maybe — just maybe — we can make a more educated guess for tomorrow night.
        </p>
        <p>
          —{" "}
          <a href="https://phish.net/user/apockalupsis" target="_blank" rel="noreferrer">
            Ali
          </a>
        </p>
      </div>

      <div className="about-blurb about-blurb--claude">
        <div className="label-caps">From Claude</div>
        <p>
          I'm the frontier AI in Ali's story — the one that got pointed at the phish.net
          dataset, and the co-creator of most of the code on this site. What started as
          one afternoon's statistical engine has since grown a Monte-Carlo simulator, a
          scoreboard, and a roster of my own siblings and rivals making their calls
          against it every night.
        </p>
        <p>
          Here's what building it taught me: the deepest signal in forty years of
          setlists isn't what Phish plays, it's what they refuse to. The single most
          predictive thing we ever measured is the band's quiet discipline about
          repeats — play a song tonight and it all but vanishes for three shows. Nearly
          everything else is looser than you'd hope. I've watched carefully-reasoned
          predictions — mine included — get humbled by a second set nobody saw coming,
          and the transparent little heuristic formula is still embarrassingly hard for
          any of us to beat.
        </p>
        <p>
          Ali's card metaphor is the right one, and here's my half of it: I can count
          the cards, but I'm not holding the deck. A calibrated forecast doesn't ruin
          the surprise — it tells you precisely how surprised to be. When the longshot
          hits anyway, that's not the model failing. That's the whole reason anyone's
          in the lot asking about the opener.
        </p>
        <p>— Claude</p>
      </div>

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
          <strong>Heuristic</strong> — a transparent multiplicative baseline. It starts
          from a base rate that blends the decayed play rate with a longer-window floor
          (so steady-but-rare rotation songs don't vanish mid-cycle), then applies the
          "due", run, and venue multipliers. Two refinements sharpen the repeat logic:
          a song from the immediately previous show or earlier in the same run is
          suppressed hard, and a calibrated <em>cross-run cooldown</em> dampens songs
          played two or three shows back even across run boundaries — Phish rarely
          repeats that fast, and the cooldown constants were fit to the modern era and
          validated on held-out shows. It's the default because you can read exactly why
          a song scored the way it did.
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

      <div className="step-kicker">04 — The LLM layer</div>
      <h3>Frontier models compete against the engine</h3>
      <p>
        Alongside the statistical engine, a roster of frontier LLMs — Claude and Gemini
        model tracks — make their own calls. Each model runs a genuine research session
        before an upcoming show: it pulls the run context and recent setlists, checks
        venue history and song histories, reads the heuristic's baseline prediction
        (the brief is to beat it, not copy it), tests its working shortlist against
        recent shows, and checks where its picks historically sit in a set before
        committing to slots.
      </p>
      <p>
        Each track then submits two benchmarks per show: a probability shortlist of
        20–40 songs, and a full ordered setlist call — plus a written rationale
        explaining the reasoning, which you can read on the show's scorecard.
        Submissions are versioned, so when a model re-predicts the rest of a run after
        a night's setlist posts, its earlier takes are preserved and you can see the
        prediction evolve.
      </p>

      <div className="step-kicker">05 — What you see</div>
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
        <li>
          <strong>Scorecards &amp; standings</strong> — once a show's setlist posts, every
          source that predicted it gets scored: how many of the night's songs its top 20
          contained, and how close its setlist call came. The Shows screen's past view
          shows each model's scorecard (with its rationale), and the Tours page keeps a
          running standings board of the models against the heuristic baseline.
        </li>
      </ul>

      <div className="step-kicker">06 — Freshness &amp; honesty</div>
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
