import { safeExternalUrl } from '../lib/api';

function StreamingBadge({ providers }) {
  const link = safeExternalUrl(providers?.link);
  const flatrate = Array.isArray(providers?.flatrate)
    ? providers.flatrate.filter((provider) => provider?.provider_name && provider?.logo_path)
    : [];

  if (!link || !flatrate.length) return null;

  return (
    <a
      className="streaming-badge"
      href={link}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Watch on ${flatrate.map((provider) => provider.provider_name).join(', ')}`}
      title={`Streaming on ${flatrate.map((provider) => provider.provider_name).join(', ')}`}
    >
      {flatrate.map((provider) => (
        <img
          key={`${provider.provider_name}-${provider.logo_path}`}
          src={`https://image.tmdb.org/t/p/w45${provider.logo_path}`}
          alt={provider.provider_name}
          className="streaming-provider-logo w-8 h-8 rounded-md shadow-sm border border-gray-700/50"
          loading="lazy"
          referrerPolicy="no-referrer"
        />
      ))}
    </a>
  );
}

export default StreamingBadge;
