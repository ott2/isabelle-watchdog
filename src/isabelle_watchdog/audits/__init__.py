"""Audits of the corpus tooling's own measurement decisions.

Not tests of the code -- tests of the *statistics*.  Each module interrogates
one judgement call the readers make, on real corpus data, and reports whether
it holds:

  oneshot       is the one-shot rate measuring proof search, or bookkeeping?
  attribution   is the attribution ladder still reaching every trajectory?
  timeouts      is a session's failure rate load, or genuine proof failure?
  zerodiff      what is a build whose recorded diff is empty?
  lengths       three ways to count a trajectory, side by side
  significance  how much of the one-shot gap survives a weaker independence
                assumption than the textbook test makes?
  loci          the error_loci path end to end, on synthetic Isabelle text

They exist because the numbers these tools produce get published, and a
filter that quietly changes what it counts is invisible in the output.  Each
one re-derives its quantity a second way rather than re-running the same code
path, so agreement is evidence rather than tautology.
"""
