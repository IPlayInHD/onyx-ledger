The CloudWatch Logs KMS key is created in the environment root, not here: the
network, cache and compute modules all need it to encrypt their log groups, and
this module needs the load balancer and database that compute and database
create. Owning the key here would make the graph circular.
